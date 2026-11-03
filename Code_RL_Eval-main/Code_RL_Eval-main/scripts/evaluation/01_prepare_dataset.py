#!/usr/bin/env python3
"""
步骤 1: 校验 case 完整性

功能：
1. 验证指定 case 是否具备评测条件
2. 检查必要文件是否齐全
3. 验证 metadata.json 内容的有效性

用法：
    python scripts/evaluation/01_prepare_dataset.py --case manual_tasks/xxx
"""

import json
import argparse
from pathlib import Path
from typing import Dict


class CaseValidator:
    """Case 完整性校验器"""

    REQUIRED_FILES = [
        "metadata.json",
        "verifier.py",
        "hard_gates.py",
        "rubric.py",
        "rubric.md",
        "prompt.md",
        "setup.py",
        "Dockerfile"
    ]

    def __init__(self, case_path: Path):
        self.case_path = case_path

    def validate_case(self) -> Dict:
        """验证 case 完整性"""
        validation_result = {
            "valid": True,
            "missing_files": [],
            "warnings": []
        }

        # 1. 检查必需文件
        for required_file in self.REQUIRED_FILES:
            file_path = self.case_path / required_file
            if not file_path.exists():
                validation_result["missing_files"].append(required_file)
                validation_result["valid"] = False

        # 2. 检查 repo 目录
        repo_dir = self.case_path / "repo"
        if not repo_dir.exists():
            validation_result["missing_files"].append("repo/")
            validation_result["valid"] = False
        elif not repo_dir.is_dir():
            validation_result["warnings"].append("repo 应该是一个目录")

        # 3. 验证 metadata.json 内容
        metadata_file = self.case_path / "metadata.json"
        if metadata_file.exists():
            try:
                with open(metadata_file) as f:
                    metadata = json.load(f)

                # 检查关键字段
                required_fields = ["repo", "base_commit", "instance_id", "test_directives"]
                for field in required_fields:
                    if field not in metadata:
                        validation_result["warnings"].append(f"metadata.json 缺少字段: {field}")

                # 检查 test_directives
                if "test_directives" in metadata:
                    test_directives = metadata["test_directives"]
                    if "install" not in test_directives:
                        validation_result["warnings"].append("test_directives 缺少 install 命令")
                    if "test" not in test_directives:
                        validation_result["warnings"].append("test_directives 缺少 test 命令")

            except json.JSONDecodeError as e:
                validation_result["valid"] = False
                validation_result["warnings"].append(f"metadata.json 格式错误: {e}")
            except Exception as e:
                validation_result["warnings"].append(f"读取 metadata.json 出错: {e}")

        return validation_result

    def print_result(self, result: Dict):
        """打印校验结果"""
        print("\n" + "=" * 80)
        print("校验结果")
        print("=" * 80)

        if result["valid"]:
            print("\n✅ Case 完整性校验通过")
        else:
            print("\n❌ Case 完整性校验失败")

        if result["missing_files"]:
            print(f"\n缺少的文件:")
            for file in result["missing_files"]:
                print(f"  ❌ {file}")

        if result["warnings"]:
            print(f"\n⚠️  警告:")
            for warning in result["warnings"]:
                print(f"  ⚠️  {warning}")

        if result["valid"]:
            print(f"\n✅ 该 case 可以进行评测")

    def run(self):
        """运行校验"""
        print("=" * 80)
        print("步骤 1: 校验 Case 完整性")
        print("=" * 80)
        print(f"\nCase 路径: {self.case_path}")

        # 检查 case 目录是否存在
        if not self.case_path.exists():
            print(f"\n❌ Case 目录不存在: {self.case_path}")
            return 1

        if not self.case_path.is_dir():
            print(f"\n❌ 路径不是一个目录: {self.case_path}")
            return 1

        # 验证 case
        result = self.validate_case()

        # 打印结果
        self.print_result(result)

        return 0 if result["valid"] else 1


def main():
    parser = argparse.ArgumentParser(description="校验 case 完整性")
    parser.add_argument(
        "--case",
        type=str,
        required=True,
        help="Case 目录路径，如: manual_tasks/xxx"
    )

    args = parser.parse_args()

    validator = CaseValidator(case_path=Path(args.case))
    return validator.run()


if __name__ == "__main__":
    import sys
    sys.exit(main())

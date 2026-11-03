#!/usr/bin/env python3
"""
Step 5: 用大模型审计并补充单元测试

功能：
1. 读取 metadata.json、verifier.py、hard_gates.py
2. 读取相关生产代码和现有测试
3. 使用大模型审计测试覆盖并生成补充测试
4. 输出 tests_audit.md 和 candidate_tests.patch
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# 添加 scripts 到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm_client import call_llm


class TestsAuditor:
    """测试审计和补充工具"""

    def __init__(self, case_path: str):
        self.case_path = Path(case_path)
        self.repo_path = self.case_path / "repo"

    def load_materials(self) -> Dict:
        """加载必要的材料"""
        materials = {}

        # 加载 metadata.json
        metadata_file = self.case_path / "metadata.json"
        if metadata_file.exists():
            with open(metadata_file, 'r', encoding='utf-8') as f:
                materials['metadata'] = json.load(f)
            print(f"✅ 已加载 metadata.json")
        else:
            print(f"⚠️  未找到 metadata.json")

        # 加载 verifier.py
        verifier_file = self.case_path / "verifier.py"
        if verifier_file.exists():
            with open(verifier_file, 'r', encoding='utf-8') as f:
                materials['verifier'] = f.read()
            print(f"✅ 已加载 verifier.py")
        else:
            print(f"⚠️  未找到 verifier.py")

        # 加载 hard_gates.py
        hard_gates_file = self.case_path / "hard_gates.py"
        if hard_gates_file.exists():
            with open(hard_gates_file, 'r', encoding='utf-8') as f:
                materials['hard_gates'] = f.read()
            print(f"✅ 已加载 hard_gates.py")
        else:
            print(f"⚠️  未找到 hard_gates.py")

        return materials

    def load_source_code(self, materials: Dict) -> str:
        """加载相关生产代码"""
        source_code = []

        if 'metadata' not in materials:
            return ""

        metadata = materials['metadata']
        editable_paths = metadata.get('editable_paths', [])

        print(f"\n📖 读取相关生产代码...")
        for path in editable_paths[:10]:  # 限制最多读取 10 个文件
            file_path = self.repo_path / path
            if file_path.exists() and file_path.is_file():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    source_code.append(f"### File: {path}\n\n```python\n{content}\n```\n")
                    print(f"  ✅ {path}")
                except Exception as e:
                    print(f"  ⚠️  无法读取 {path}: {e}")

        return "\n".join(source_code)

    def find_test_files(self, materials: Dict) -> List[str]:
        """查找相关测试文件"""
        if 'metadata' not in materials:
            return []

        metadata = materials['metadata']

        # 从 metadata 中获取测试路径
        test_paths = []

        # 从 test_command 中提取测试文件路径
        test_command = metadata.get('test_command', '')
        if 'pytest' in test_command:
            # 提取 pytest 后的文件路径
            parts = test_command.split()
            for part in parts:
                if part.startswith('tests/') and not part.startswith('-'):
                    test_paths.append(part)

        # 如果没有从命令中找到，尝试查找 frozen_paths 中的测试文件
        if not test_paths:
            frozen_paths = metadata.get('frozen_paths', [])
            for path in frozen_paths:
                if 'test' in path.lower():
                    test_paths.append(path)

        return test_paths

    def load_test_files(self, test_paths: List[str]) -> str:
        """加载测试文件内容"""
        test_code = []

        print(f"\n📝 读取现有测试文件...")
        for path in test_paths[:5]:  # 限制最多读取 5 个测试文件
            file_path = self.repo_path / path
            if file_path.exists() and file_path.is_file():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    test_code.append(f"### Test File: {path}\n\n```python\n{content}\n```\n")
                    print(f"  ✅ {path}")
                except Exception as e:
                    print(f"  ⚠️  无法读取 {path}: {e}")

        return "\n".join(test_code)

    def load_prompt_template(self) -> str:
        """从 prompts 目录加载提示词模板"""
        script_dir = Path(__file__).parent.parent
        prompt_file = script_dir / "prompts" / "tests_audit_generation.txt"

        if not prompt_file.exists():
            raise FileNotFoundError(f"未找到提示词文件: {prompt_file}")

        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()

    def build_llm_prompt(self, materials: Dict, source_code: str, test_code: str) -> str:
        """构建给大模型的 prompt"""
        # 加载提示词模板
        template = self.load_prompt_template()

        # 构建材料部分
        materials_parts = []

        # 添加 metadata
        if 'metadata' in materials:
            materials_parts.append("\n## metadata.json\n")
            materials_parts.append("```json\n")
            materials_parts.append(json.dumps(materials['metadata'], indent=2, ensure_ascii=False))
            materials_parts.append("\n```\n")

        # 添加 verifier
        if 'verifier' in materials:
            materials_parts.append("\n## verifier.py\n")
            materials_parts.append("```python\n")
            materials_parts.append(materials['verifier'])
            materials_parts.append("\n```\n")

        # 添加 hard_gates
        if 'hard_gates' in materials:
            materials_parts.append("\n## hard_gates.py\n")
            materials_parts.append("```python\n")
            materials_parts.append(materials['hard_gates'])
            materials_parts.append("\n```\n")

        # 添加生产代码
        if source_code:
            materials_parts.append("\n## 相关生产代码\n")
            materials_parts.append(source_code)

        # 添加测试代码
        if test_code:
            materials_parts.append("\n## 现有测试\n")
            materials_parts.append(test_code)

        # 替换模板中的占位符
        materials_content = "".join(materials_parts)
        prompt = template.replace("{materials}", materials_content)

        return prompt

    def generate_with_llm(self, prompt: str) -> str:
        """使用大模型生成审计报告和测试补丁"""
        print("\n🤖 正在使用大模型审计测试并生成补充...")

        response = call_llm(prompt, temperature=0.0)

        print("✅ 生成完成")
        return response

    def parse_response(self, response: str) -> Tuple[str, str]:
        """解析响应，分离 tests_audit.md 和 candidate_tests.patch"""
        audit_content = ""
        patch_content = ""

        # 查找分隔符
        if "=== tests_audit.md ===" in response:
            parts = response.split("=== tests_audit.md ===")
            if len(parts) > 1:
                remaining = parts[1]

                if "=== candidate_tests.patch ===" in remaining:
                    audit_part, patch_part = remaining.split("=== candidate_tests.patch ===")
                    audit_content = audit_part.strip()
                    patch_content = patch_part.strip()
                else:
                    audit_content = remaining.strip()
        else:
            # 如果没有分隔符，尝试其他方式解析
            audit_content = response.strip()

        return audit_content, patch_content

    def save_outputs(self, audit_content: str, patch_content: str):
        """保存输出文件"""
        # 保存 tests_audit.md
        audit_file = self.case_path / "tests_audit.md"
        with open(audit_file, 'w', encoding='utf-8') as f:
            f.write(audit_content)
        print(f"\n✅ 已保存 tests_audit.md: {audit_file}")

        # 保存 candidate_tests.patch（如果有内容）
        if patch_content and patch_content != "无需补充测试":
            patch_file = self.case_path / "candidate_tests.patch"
            with open(patch_file, 'w', encoding='utf-8') as f:
                f.write(patch_content)
            print(f"✅ 已保存 candidate_tests.patch: {patch_file}")
        else:
            print(f"ℹ️  无需补充测试")

    def run(self):
        """运行审计流程"""
        print("=" * 80)
        print("Step 5: 审计并补充单元测试")
        print("=" * 80)

        # 1. 加载材料
        print("\n📚 加载材料...")
        materials = self.load_materials()

        if not materials:
            print("❌ 未找到任何材料，无法进行审计")
            return 1

        # 2. 读取生产代码
        source_code = self.load_source_code(materials)

        # 3. 查找并读取测试文件
        test_paths = self.find_test_files(materials)
        test_code = self.load_test_files(test_paths)

        # 4. 构建 LLM prompt
        llm_prompt = self.build_llm_prompt(materials, source_code, test_code)

        # 5. 使用大模型生成
        try:
            response = self.generate_with_llm(llm_prompt)
        except Exception as e:
            print(f"❌ 生成失败: {e}")
            return 1

        # 6. 解析响应
        audit_content, patch_content = self.parse_response(response)

        # 7. 保存输出
        self.save_outputs(audit_content, patch_content)

        # 8. 总结
        print("\n" + "=" * 80)
        print("审计完成")
        print("=" * 80)
        print(f"\n下一步：")
        print(f"1. 查看审计报告: {self.case_path}/tests_audit.md")
        if patch_content and patch_content != "无需补充测试":
            print(f"2. 查看测试补丁: {self.case_path}/candidate_tests.patch")
            print(f"3. 应用补丁: cd {self.case_path}/repo && git apply ../candidate_tests.patch")
            print(f"4. 运行测试验证补丁")
        print(f"5. 确认无误后进入第六步")

        return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Step 5: 审计并补充单元测试')
    parser.add_argument('--case', required=True, help='任务目录路径，如: manual_tasks/fastapi__fastapi__c48e67b__16013')

    args = parser.parse_args()

    auditor = TestsAuditor(args.case)
    return auditor.run()


if __name__ == '__main__':
    sys.exit(main())

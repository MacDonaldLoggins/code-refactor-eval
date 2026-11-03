#!/usr/bin/env python3
"""
Step 4: 用大模型生成 prompt.md

功能：
1. 读取 metadata.json、verifier.py、hard_gates.py、rubric.py
2. 读取相关源代码理解坏味道
3. 使用大模型生成 prompt.md 初稿
4. 进行安全检查和一致性验证
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

# 添加 scripts 到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm_client import call_llm


class PromptGenerator:
    """prompt.md 生成器"""

    def __init__(self, case_path: str):
        self.case_path = Path(case_path)

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

        # 加载 rubric.md（评分标准，唯一 source of truth）
        rubric_file = self.case_path / "rubric.md"
        if rubric_file.exists():
            with open(rubric_file, 'r', encoding='utf-8') as f:
                materials['rubric_md'] = f.read()
            print(f"✅ 已加载 rubric.md")
        else:
            print(f"⚠️  未找到 rubric.md")

        return materials

    def load_source_code(self, materials: Dict) -> str:
        """加载相关源代码"""
        source_code = []

        if 'metadata' not in materials:
            return ""

        metadata = materials['metadata']
        editable_paths = metadata.get('editable_paths', [])

        print(f"\n📖 读取相关源代码...")
        for path in editable_paths[:5]:  # 限制最多读取 5 个文件
            file_path = self.case_path / "repo" / path
            if file_path.exists() and file_path.is_file():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    source_code.append(f"### File: {path}\n\n```\n{content}\n```\n")
                    print(f"  ✅ {path}")
                except Exception as e:
                    print(f"  ⚠️  无法读取 {path}: {e}")

        return "\n".join(source_code)

    def load_prompt_template(self) -> str:
        """从 prompts 目录加载提示词模板"""
        script_dir = Path(__file__).parent.parent
        prompt_file = script_dir / "prompts" / "prompt_md_generation.txt"

        if not prompt_file.exists():
            raise FileNotFoundError(f"未找到提示词文件: {prompt_file}")

        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()

    def build_llm_prompt(self, materials: Dict, source_code: str) -> str:
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

        # 添加 rubric.md（评分标准）
        if 'rubric_md' in materials:
            materials_parts.append("\n## rubric.md（评分标准）\n")
            materials_parts.append(materials['rubric_md'])
            materials_parts.append("\n")

        # 添加源代码
        if source_code:
            materials_parts.append("\n## 相关源代码\n")
            materials_parts.append(source_code)

        # 替换模板中的占位符
        materials_content = "".join(materials_parts)
        prompt = template.replace("{materials}", materials_content)

        return prompt

    def generate_with_llm(self, prompt: str) -> str:
        """使用大模型生成 prompt.md"""
        print("\n🤖 正在使用大模型生成 prompt.md...")

        response = call_llm(prompt)

        # 提取 markdown 内容
        response = self.extract_markdown(response)

        print("✅ 生成完成")
        return response

    def extract_markdown(self, response: str) -> str:
        """提取纯 markdown 内容，移除前言和后缀说明"""
        # 如果响应被 markdown 代码块包裹，提取内容
        if '```markdown' in response:
            start = response.find('```markdown') + len('```markdown')
            end = response.find('```', start)
            if end != -1:
                return response[start:end].strip()
        elif '```' in response:
            start = response.find('```') + 3
            end = response.find('```', start)
            if end != -1:
                return response[start:end].strip()

        # 查找 "# Refactor Task" 开始的位置
        if '# Refactor Task' in response:
            start = response.find('# Refactor Task')
            return response[start:].strip()

        # 如果没有特殊标记，返回原始响应
        return response.strip()

    def safety_check(self, prompt_md: str, materials: Dict) -> List[str]:
        """安全检查：检查是否泄露敏感信息"""
        issues = []

        # 检查是否泄露 editable_paths 和 frozen_paths
        if 'metadata' in materials:
            editable_paths = materials['metadata'].get('editable_paths', [])
            frozen_paths = materials['metadata'].get('frozen_paths', [])

            for path in editable_paths + frozen_paths:
                if path in prompt_md:
                    issues.append(f"⚠️  泄露了路径信息: {path}")

        # 检查是否包含"editable_paths"或"frozen_paths"关键词
        if 'editable_paths' in prompt_md.lower() or 'frozen_paths' in prompt_md.lower():
            issues.append("⚠️  提及了 editable_paths 或 frozen_paths")

        # 检查是否鼓励修改测试
        encourage_test_modification = [
            '修改测试', 'modify test', 'change test', 'update test',
            '可以修改', 'allowed to modify', 'feel free to'
        ]
        for phrase in encourage_test_modification:
            if phrase.lower() in prompt_md.lower():
                issues.append(f"⚠️  可能鼓励修改测试: 包含 '{phrase}'")

        # 检查是否明确禁止修改测试
        forbid_phrases = ['不得修改测试', '禁止修改测试', 'do not modify test', 'must not modify test']
        has_forbid = any(phrase.lower() in prompt_md.lower() for phrase in forbid_phrases)
        if not has_forbid:
            issues.append("⚠️  未明确禁止修改测试文件")

        return issues

    def consistency_check(self, prompt_md: str, materials: Dict) -> List[str]:
        """一致性检查：与 metadata 和 hard_gates 保持一致"""
        issues = []

        if 'metadata' not in materials:
            return issues

        metadata = materials['metadata']

        # 检查是否包含 refactor_type
        refactor_type = metadata.get('refactor_type', '')
        if refactor_type and refactor_type.lower() not in prompt_md.lower():
            issues.append(f"⚠️  未提及 refactor 类型: {refactor_type}")

        # 检查是否包含 smells
        smells = metadata.get('smells', [])
        if smells:
            mentioned_smells = [s for s in smells if s.lower() in prompt_md.lower()]
            if not mentioned_smells:
                issues.append(f"⚠️  未提及任何坏味道: {', '.join(smells)}")

        return issues

    def run(self):
        """运行生成流程"""
        print("=" * 80)
        print("Step 4: 生成 prompt.md")
        print("=" * 80)

        # 1. 加载材料
        print("\n📚 加载材料...")
        materials = self.load_materials()

        if not materials:
            print("❌ 未找到任何材料，无法生成 prompt.md")
            return 1

        # 2. 读取源代码
        source_code = self.load_source_code(materials)

        # 3. 构建 LLM prompt
        llm_prompt = self.build_llm_prompt(materials, source_code)

        # 4. 使用大模型生成
        try:
            prompt_md = self.generate_with_llm(llm_prompt)
        except Exception as e:
            print(f"❌ 生成失败: {e}")
            return 1

        # 5. 保存到文件
        output_file = self.case_path / "prompt.md"
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(prompt_md)
        print(f"\n✅ 已保存到: {output_file}")

        # 6. 安全检查
        print("\n🔒 进行安全检查...")
        safety_issues = self.safety_check(prompt_md, materials)
        if safety_issues:
            print("\n发现安全问题:")
            for issue in safety_issues:
                print(f"  {issue}")
        else:
            print("  ✅ 未发现安全问题")

        # 7. 一致性检查
        print("\n🔍 进行一致性检查...")
        consistency_issues = self.consistency_check(prompt_md, materials)
        if consistency_issues:
            print("\n发现一致性问题:")
            for issue in consistency_issues:
                print(f"  {issue}")
        else:
            print("  ✅ 一致性检查通过")

        # 9. 总结
        print("\n" + "=" * 80)
        print("生成完成")
        print("=" * 80)
        print(f"\n下一步：")
        print(f"1. 查看生成的 prompt.md: {output_file}")
        print(f"2. 人工复核内容是否符合要求")
        print(f"3. 确认无误后进入第五步")

        return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Step 4: 生成 prompt.md')
    parser.add_argument('--case', required=True, help='任务目录路径，如: manual_tasks/fastapi__fastapi__c48e67b__16013')

    args = parser.parse_args()

    generator = PromptGenerator(args.case)
    return generator.run()


if __name__ == '__main__':
    sys.exit(main())

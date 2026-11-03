#!/usr/bin/env python3
"""
Step 7: 生成 Dockerfile

功能：
1. 读取 metadata.json 和项目结构
2. 使用大模型生成 Dockerfile
3. 注入镜像源 / Claude Code 安装 / setuptools_scm 兜底等逻辑

说明：setup.py 的生成已拆分到 06_generate_setup.py。
"""

import json
import sys
from pathlib import Path
from typing import Dict

# 添加 scripts 到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm_client import call_llm


class DockerfileGenerator:
    """Dockerfile 生成器"""

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

    def detect_project_structure(self) -> Dict:
        """检测项目结构和依赖文件"""
        structure = {
            "dependency_files": [],
            "config_files": [],
            "has_git": False
        }

        # 检测依赖文件
        dependency_patterns = [
            "requirements.txt",
            "setup.py",
            "pyproject.toml",
            "Pipfile",
            "package.json",
            "go.mod",
            "Cargo.toml",
            "pom.xml",
            "build.gradle"
        ]

        for pattern in dependency_patterns:
            dep_file = self.repo_path / pattern
            if dep_file.exists():
                structure["dependency_files"].append(pattern)

        # 检测配置文件
        config_patterns = [
            "pytest.ini",
            "setup.cfg",
            "tox.ini",
            ".eslintrc",
            "jest.config.js"
        ]

        for pattern in config_patterns:
            config_file = self.repo_path / pattern
            if config_file.exists():
                structure["config_files"].append(pattern)

        # 检测 git
        git_dir = self.repo_path / ".git"
        structure["has_git"] = git_dir.exists()

        print(f"📂 检测到依赖文件: {', '.join(structure['dependency_files']) or '无'}")
        print(f"📂 检测到配置文件: {', '.join(structure['config_files']) or '无'}")
        print(f"📂 Git 仓库: {'是' if structure['has_git'] else '否'}")

        return structure

    def load_prompt_template(self) -> str:
        """从 prompts 目录加载提示词模板"""
        script_dir = Path(__file__).parent.parent
        prompt_file = script_dir / "prompts" / "dockerfile_generation.txt"

        if not prompt_file.exists():
            raise FileNotFoundError(f"未找到提示词文件: {prompt_file}")

        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()

    def build_llm_prompt(self, metadata: Dict, structure: Dict) -> str:
        """构建给大模型的 prompt"""
        # 加载提示词模板
        template = self.load_prompt_template()

        # 构建材料部分
        materials_parts = []

        # 添加 metadata
        materials_parts.append("\n## metadata.json\n")
        materials_parts.append("```json\n")
        materials_parts.append(json.dumps(metadata, indent=2, ensure_ascii=False))
        materials_parts.append("\n```\n")

        # 添加项目结构信息
        materials_parts.append("\n## 项目结构\n")
        materials_parts.append(f"- 依赖文件: {', '.join(structure['dependency_files']) or '无'}\n")
        materials_parts.append(f"- 配置文件: {', '.join(structure['config_files']) or '无'}\n")
        materials_parts.append(f"- Git 仓库: {'是' if structure['has_git'] else '否'}\n")

        # 替换模板中的占位符
        materials_content = "".join(materials_parts)
        prompt = template.replace("{materials}", materials_content)

        return prompt

    def generate_dockerfile_with_llm(self, prompt: str) -> str:
        """使用大模型生成 Dockerfile"""
        print("\n🤖 正在使用大模型生成 Dockerfile...")

        response = call_llm(prompt, temperature=0.0)

        # 提取 Dockerfile 内容
        response = self.extract_dockerfile(response)

        print("✅ 生成完成")
        return response

    def extract_dockerfile(self, response: str) -> str:
        """提取 Dockerfile 内容"""
        # 如果响应被 markdown 代码块包裹，提取内容
        if '```dockerfile' in response:
            start = response.find('```dockerfile') + len('```dockerfile')
            end = response.find('```', start)
            if end != -1:
                return response[start:end].strip()
        elif '```Dockerfile' in response:
            start = response.find('```Dockerfile') + len('```Dockerfile')
            end = response.find('```', start)
            if end != -1:
                return response[start:end].strip()
        elif '```' in response:
            start = response.find('```') + 3
            end = response.find('```', start)
            if end != -1:
                return response[start:end].strip()

        # 查找 FROM 开始的位置
        if 'FROM ' in response:
            start = response.find('FROM ')
            return response[start:].strip()

        return response.strip()

    def save_dockerfile(self, dockerfile: str):
        """保存 Dockerfile 并注入必要的构建逻辑"""
        # 注入 Claude Code 安装逻辑到 Dockerfile
        dockerfile = self.inject_claude_code_installation(dockerfile)

        # 注入 Codex 安装逻辑到 Dockerfile
        dockerfile = self.inject_codex_installation(dockerfile)

        # 注入 setuptools_scm 版本兜底（repo/ 无 .git 时无法从 git 推断版本）
        dockerfile = self.inject_setuptools_scm_workaround(dockerfile)

        # 移除对项目自身的 editable 安装（源码通过 COPY + PYTHONPATH 使用，无需构建安装）
        dockerfile = self._strip_project_self_install(dockerfile)

        # 补齐 setup.py / prompt.md 到 /workspace（评测运行期必需，LLM 常遗漏）
        dockerfile = self._ensure_eval_runtime_files_copied(dockerfile)

        # 保存 Dockerfile
        dockerfile_path = self.case_path / "Dockerfile"
        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.write(dockerfile)
        print(f"\n✅ 已保存 Dockerfile: {dockerfile_path}")

        # 复制 install_claude_code.sh 脚本到任务目录
        self.copy_claude_install_script()

    def inject_claude_code_installation(self, dockerfile: str) -> str:
        """在 Dockerfile 的 FROM 语句后注入 Claude Code 安装逻辑和国内镜像源配置"""
        lines = dockerfile.split('\n')
        result = []
        mirror_injected = False
        claude_injected = False

        # 镜像源配置片段
        mirror_config = """
# ==========================================================================
# China mirror configuration
# All package sources use domestic mirrors (Tsinghua apt/pip, Taobao npm),
# so NO proxy is needed. Docker Desktop may inject an unreachable host proxy
# (e.g. a local clash listening on a LAN IP) into HTTP_PROXY/HTTPS_PROXY —
# clear them globally so apt/pip/npm/curl all connect directly.
# ==========================================================================
ENV HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
    ALL_PROXY="" all_proxy="" NO_PROXY="*" no_proxy="*"

# Disable proxy for apt as a double safeguard (apt has its own proxy config)
RUN printf 'Acquire::http::Proxy "false";\\nAcquire::https::Proxy "false";\\n' > /etc/apt/apt.conf.d/99-no-proxy

# Switch Debian apt source to Tsinghua mirror
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list 2>/dev/null || true

# Configure pip to use Tsinghua mirror
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
"""

        # Claude Code 安装片段（跳过 apt 安装，依赖已在前面安装）
        # 通过淘宝 npm 源安装，全程国内源、无需代理
        claude_code_install = """
# Install Node.js + npm (Tsinghua apt mirror; proxy disabled globally above)
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI via Taobao npm mirror (no proxy needed, dependencies already installed)
COPY install_claude_code.sh /tmp/install_claude_code.sh
RUN SKIP_APT_INSTALL=1 CLAUDE_INSTALL_METHOD=npm bash /tmp/install_claude_code.sh && rm -f /tmp/install_claude_code.sh
ENV PATH="/root/.local/bin:${PATH}"

# Preload Claude Code settings (gateway base_url / token / model) into ~/.claude
COPY settings.json /root/.claude/settings.json
"""

        i = 0
        while i < len(lines):
            line = lines[i]
            result.append(line)

            # 在第一个 FROM 语句后注入镜像源配置
            if not mirror_injected and line.strip().startswith('FROM '):
                result.append(mirror_config)
                mirror_injected = True

            # 在包含 apt-get install 和系统依赖的 RUN 块之后注入 Claude Code
            # 寻找包含 rm -rf /var/lib/apt/lists 的行（apt 清理，表示 apt-get 块结束）
            if not claude_injected and 'rm -rf /var/lib/apt/lists' in line:
                result.append(claude_code_install)
                claude_injected = True

            i += 1

        # 如果没有找到合适的位置注入 Claude Code，在第一个 COPY 之前插入
        if not claude_injected:
            final_result = []
            for line in result:
                if line.strip().startswith('COPY '):
                    final_result.append(claude_code_install)
                    claude_injected = True
                final_result.append(line)
            result = final_result

        return '\n'.join(result)

    def inject_codex_installation(self, dockerfile: str) -> str:
        """在 Claude Code 安装之后注入 Codex 安装逻辑"""
        lines = dockerfile.split('\n')
        result = []
        codex_injected = False

        # Codex 安装片段（跳过 apt 安装，依赖已在前面安装，与 Claude Code 共用 Node.js/npm）
        codex_install = """
# Install OpenAI Codex CLI via Taobao npm mirror (no proxy needed, dependencies already installed)
COPY install_codex.sh /tmp/install_codex.sh
RUN SKIP_APT_INSTALL=1 bash /tmp/install_codex.sh && rm -f /tmp/install_codex.sh

# Preload Codex settings (gateway base_url / model) into ~/.codex
RUN mkdir -p /root/.codex
COPY codex_config.toml /root/.codex/config.toml
"""

        i = 0
        while i < len(lines):
            line = lines[i]
            result.append(line)

            # 在 Claude Code settings.json COPY 语句之后注入 Codex
            if not codex_injected and 'COPY settings.json /root/.claude/settings.json' in line:
                result.append(codex_install)
                codex_injected = True

            i += 1

        # 如果没有找到 Claude Code 配置，在第一个 WORKDIR 之前插入
        if not codex_injected:
            final_result = []
            for line in result:
                if line.strip().startswith('WORKDIR '):
                    final_result.append(codex_install)
                    codex_injected = True
                final_result.append(line)
            result = final_result

        return '\n'.join(result)

    def copy_claude_install_script(self):
        """复制 Claude Code 和 Codex 安装脚本及配置到任务目录"""
        script_dir = Path(__file__).parent.parent
        import shutil

        # Claude Code 安装脚本
        source_script = script_dir / "tools" / "install_claude_code.sh"
        target_script = self.case_path / "install_claude_code.sh"
        if not source_script.exists():
            print(f"⚠️  警告: 找不到 Claude Code 安装脚本: {source_script}")
        else:
            shutil.copy2(source_script, target_script)
            print(f"✅ 已复制 Claude Code 安装脚本: {target_script}")

        # Claude Code 配置文件 settings.json（含网关 base_url / token / 模型），Dockerfile 会 COPY 到 /root/.claude/
        source_settings = script_dir / "tools" / "settings.json"
        target_settings = self.case_path / "settings.json"
        if not source_settings.exists():
            print(f"⚠️  警告: 找不到 Claude Code 配置文件: {source_settings}")
        else:
            shutil.copy2(source_settings, target_settings)
            print(f"✅ 已复制 Claude Code 配置: {target_settings}")

        # Codex 安装脚本
        source_codex_script = script_dir / "tools" / "install_codex.sh"
        target_codex_script = self.case_path / "install_codex.sh"
        if not source_codex_script.exists():
            print(f"⚠️  警告: 找不到 Codex 安装脚本: {source_codex_script}")
        else:
            shutil.copy2(source_codex_script, target_codex_script)
            print(f"✅ 已复制 Codex 安装脚本: {target_codex_script}")

        # Codex 配置文件 codex_config.toml（含网关 base_url / 模型），Dockerfile 会 COPY 到 /root/.codex/
        source_codex_config = script_dir / "tools" / "codex_config.toml"
        target_codex_config = self.case_path / "codex_config.toml"
        if not source_codex_config.exists():
            print(f"⚠️  警告: 找不到 Codex 配置文件: {source_codex_config}")
        else:
            shutil.copy2(source_codex_config, target_codex_config)
            print(f"✅ 已复制 Codex 配置: {target_codex_config}")

    def _needs_setuptools_scm_workaround(self) -> bool:
        """检测 repo 是否使用 setuptools_scm 但缺少 .git（无法从 git 推断版本）"""
        # 已有完整 .git 目录则能正常推断版本，无需兜底
        if (self.repo_path / ".git").exists():
            return False

        # 检查构建配置里是否使用 setuptools_scm
        for fname in ("pyproject.toml", "setup.py", "setup.cfg"):
            f = self.repo_path / fname
            if not f.exists():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "setuptools_scm" in content or "setuptools-scm" in content:
                return True
        return False

    def _guess_scm_version(self) -> str:
        """为 setuptools_scm 提供一个占位版本号

        repo/ 缺少 .git 且 metadata 无可靠版本信息时，返回占位版本。
        评测只关注代码重构质量，版本字符串本身不影响功能测试。
        """
        metadata_file = self.case_path / "metadata.json"
        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            version = metadata.get("version")
            if isinstance(version, str) and version.strip():
                return version.strip()
        except (OSError, ValueError):
            pass
        return "0.0.0"

    def inject_setuptools_scm_workaround(self, dockerfile: str) -> str:
        """当 repo 使用 setuptools_scm 却无 .git 时，注入 SETUPTOOLS_SCM_PRETEND_VERSION

        setuptools_scm 依赖 git 标签推断版本，repo/ 经 COPY 后缺少 .git 会导致
        `pip install -e .` 报 "unable to detect version" 而构建失败。
        设置 SETUPTOOLS_SCM_PRETEND_VERSION 可让其跳过 git 检测、直接使用占位版本。
        """
        if not self._needs_setuptools_scm_workaround():
            return dockerfile

        version = self._guess_scm_version()
        env_block = (
            "\n# setuptools_scm fallback: repo/ has no .git, cannot infer version "
            "from git tags\n"
            f'ENV SETUPTOOLS_SCM_PRETEND_VERSION="{version}"\n'
        )

        lines = dockerfile.split('\n')
        result = []
        injected = False
        for line in lines:
            result.append(line)
            # 在第一个 FROM 语句后注入（ENV 全局生效，pip 构建时即可读取）
            if not injected and line.strip().startswith('FROM '):
                result.append(env_block)
                injected = True

        print(f"✅ 检测到 setuptools_scm 且 repo 无 .git，"
              f"已注入 SETUPTOOLS_SCM_PRETEND_VERSION={version}")
        return '\n'.join(result)

    def _strip_project_self_install(self, dockerfile: str) -> str:
        """移除 Dockerfile 中对项目自身的 pip 安装步骤

        项目是纯源码，通过 COPY repo/ + PYTHONPATH=/workspace/repo 即可 import，
        无需 `pip install .` / `pip install -e .` 构建安装（会触发 setuptools_scm
        等构建期版本推断问题）。这里确定性地移除这类 RUN 指令，作为 prompt 的兜底。
        安装第三方依赖（-r requirements.txt）和升级 pip/setuptools/wheel 的步骤会保留。
        """
        lines = dockerfile.split('\n')

        # 将 Dockerfile 按逻辑指令分组（处理行尾 \ 续行）
        instructions = []
        current = []
        for line in lines:
            current.append(line)
            if not line.rstrip().endswith('\\'):
                instructions.append(current)
                current = []
        if current:
            instructions.append(current)

        result_lines = []
        removed = False
        for instr in instructions:
            text = '\n'.join(instr)
            if self._is_project_self_install(text):
                removed = True
                continue
            result_lines.extend(instr)

        if removed:
            print("✅ 已移除项目自身的 pip install/editable 安装步骤"
                  "（源码通过 COPY + PYTHONPATH 使用）")

        return '\n'.join(result_lines)

    def _is_project_self_install(self, instruction_text: str) -> bool:
        """判断一条 RUN 指令是否是「安装项目自身」（editable 或当前目录安装）"""
        import re

        stripped = instruction_text.lstrip()
        if not stripped.startswith('RUN'):
            return False
        if 'pip install' not in instruction_text:
            return False
        # 安装第三方依赖清单的步骤必须保留
        if 'requirements' in instruction_text:
            return False

        # 匹配 editable 安装（-e）或安装当前目录（. / ".[extra]" / '.[extra]'）
        patterns = [
            r'pip\s+install[^\n]*\s-e\b',          # pip install ... -e
            r'pip\s+install[^\n]*["\']\.\[',       # pip install ".[dev]"
            r'pip\s+install[^\n]*\s\.\[',          # pip install .[dev]
            r'pip\s+install[^\n]*\s\.(\s|$)',      # pip install .  (末尾或空格)
        ]
        for pattern in patterns:
            if re.search(pattern, instruction_text):
                return True
        return False

    def _ensure_eval_runtime_files_copied(self, dockerfile: str) -> str:
        """确保 setup.py 和 prompt.md 被 COPY 到 /workspace 根目录

        评测运行时会在容器内执行 `python /workspace/setup.py --case /workspace`
        创建工作空间，该脚本又会读取 `/workspace/prompt.md`。若 Dockerfile 遗漏
        这两个文件，容器内会因文件不存在而失败。LLM 生成的 Dockerfile 通常只
        COPY 了 verifier.py 等评测脚本，这里确定性地补齐，作为 prompt 的兜底。

        注意：这里的 `setup.py` 指任务目录根部的「工作空间准备脚本」（06 生成），
        与 repo/setup.py（项目自身构建脚本）不是同一个文件；COPY 路径相对构建
        上下文根目录，因此不会与 repo/ 内的同名文件冲突。
        """
        required = ["setup.py", "prompt.md", "rubric.md"]

        lines = dockerfile.split('\n')

        # 优先：找到已把评测脚本 COPY 到 /workspace 根的那条指令，就地补齐缺失文件
        eval_markers = ("verifier.py", "hard_gates.py",
                        "objective_metrics.py", "rubric.py", "rubric.md")
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("COPY "):
                continue
            if not any(marker in stripped for marker in eval_markers):
                continue

            tokens = stripped.split()
            dest = tokens[-1]          # 最后一个 token 是目标目录
            srcs = tokens[1:-1]        # 中间是源文件列表
            missing = [f for f in required if f not in srcs]
            if not missing:
                return dockerfile      # 已包含，无需修改

            indent = line[:len(line) - len(line.lstrip())]
            lines[idx] = f"{indent}COPY {' '.join(srcs + missing)} {dest}"
            print(f"✅ 已在评测脚本 COPY 中补齐: {', '.join(missing)}")
            return '\n'.join(lines)

        # 兜底：未找到评测脚本 COPY，单独插入一条 COPY 指令
        to_copy = [
            f for f in required
            if not any(f in l and l.strip().startswith("COPY ") for l in lines)
        ]
        if not to_copy:
            return dockerfile

        copy_line = f"COPY {' '.join(to_copy)} /workspace/"
        # 在第一条 CMD/ENTRYPOINT 之前插入；否则追加到末尾
        insert_at = next(
            (i for i, l in enumerate(lines)
             if l.strip().startswith(("CMD", "ENTRYPOINT"))),
            None
        )
        if insert_at is not None:
            lines.insert(insert_at, copy_line)
        else:
            lines.append(copy_line)
        print(f"✅ 已插入 COPY 指令补齐评测运行文件: {', '.join(to_copy)}")
        return '\n'.join(lines)

    def run(self):
        """运行生成流程"""
        print("=" * 80)
        print("Step 7: 生成 Dockerfile")
        print("=" * 80)

        # 1. 加载 metadata
        print("\n📚 加载材料...")
        metadata = self.load_metadata()

        # 2. 检测项目结构
        print("\n🔍 检测项目结构...")
        structure = self.detect_project_structure()

        # 3. 生成 Dockerfile
        llm_prompt = self.build_llm_prompt(metadata, structure)
        try:
            dockerfile = self.generate_dockerfile_with_llm(llm_prompt)
        except Exception as e:
            print(f"❌ 生成 Dockerfile 失败: {e}")
            return 1

        # 4. 保存输出
        self.save_dockerfile(dockerfile)

        # 5. 总结
        print("\n" + "=" * 80)
        print("生成完成")
        print("=" * 80)
        print(f"\n下一步：")
        print(f"1. 查看生成的 Dockerfile: {self.case_path}/Dockerfile")
        print(f"2. 构建 Docker 镜像: cd {self.case_path} && docker build -t refactor-eval .")

        return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Step 7: 生成 Dockerfile')
    parser.add_argument('--case', required=True, help='任务目录路径，如: manual_tasks/fastapi__fastapi__c48e67b__16013')

    args = parser.parse_args()

    generator = DockerfileGenerator(args.case)
    return generator.run()


if __name__ == '__main__':
    sys.exit(main())

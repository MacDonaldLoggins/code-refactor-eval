#!/usr/bin/env python3
"""
步骤 2: 构建评测镜像

设计思路：
    每个 case 目录里都自带一个自包含的 Dockerfile，它直接 COPY repo/ 使用本地代码，
    并根据 repo 内的 requirements.txt / pyproject.toml 安装依赖，再拷入 verifier 等评测脚本。

功能：
    1. 校验 case 目录结构（Dockerfile / repo / metadata.json）
    2. 使用 case 自带 Dockerfile 构建评测镜像
    3. 显示镜像信息

用法：
    python scripts/evaluation/02_build_image.py --case manual_tasks/xxx
    python scripts/evaluation/02_build_image.py --case manual_tasks/xxx --force-rebuild
"""

import sys
import json
import argparse
import subprocess
from pathlib import Path
from typing import Dict

# 添加 scripts 到 path，以便复用 utils.llm_client
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm_client import call_llm


# docker build 失败时，允许 LLM 尝试修复 Dockerfile 的最大次数
MAX_LLM_FIX_ATTEMPTS = 3


class _BuildFailed(Exception):
    """docker build 失败，携带退出码与合并后的构建日志。"""

    def __init__(self, returncode: int, log: str):
        super().__init__(f"docker build failed with code {returncode}")
        self.returncode = returncode
        self.log = log

# Claude Code 安装 / Codex 安装 / 镜像源 / 评测脚本相关的关键标记，
# LLM 修复 Dockerfile 后必须仍然保留这些，否则拒绝该次修复。
REQUIRED_DOCKERFILE_MARKERS = [
    "install_claude_code.sh",
    "CLAUDE_INSTALL_METHOD=npm",
    "/root/.claude/settings.json",
    "install_codex.sh",
    "/root/.codex/config.toml",
]


class ImageBuilder:
    """评测镜像构建器（直接使用 case 自带 Dockerfile）"""

    def __init__(self, case_path: Path, force_rebuild: bool):
        self.case_path = case_path
        self.force_rebuild = force_rebuild

        self.dockerfile = case_path / "Dockerfile"
        self.metadata = self._load_metadata()

        # 最终镜像名（Docker 要求 repository name 必须全小写，case 目录名可能含大写）
        self.final_image_tag = f"refactor-eval/{self.case_path.name}".lower()

    def _load_metadata(self) -> Dict:
        metadata_file = self.case_path / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"未找到 metadata.json: {metadata_file}")
        with open(metadata_file) as f:
            return json.load(f)

    def _validate(self):
        if not self.dockerfile.exists():
            raise FileNotFoundError(
                f"未找到 Dockerfile: {self.dockerfile}\n"
                "  该脚本依赖 case 目录内自带的 Dockerfile（由数据合成阶段生成）。"
            )
        repo_dir = self.case_path / "repo"
        if not repo_dir.exists():
            raise FileNotFoundError(f"未找到本地 repo 目录: {repo_dir}")

    def _image_exists(self, tag: str) -> bool:
        result = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
        )
        return result.returncode == 0

    def run(self) -> int:
        print("=" * 80)
        print(f"步骤 2: 构建镜像并启动容器 - {self.case_path.name}")
        print("=" * 80)

        try:
            self._validate()

            if not self.force_rebuild and self._image_exists(self.final_image_tag):
                print(f"\n✅ 镜像已存在: {self.final_image_tag}")
                print("  如需重新构建，请使用 --force-rebuild 参数")
            else:
                print("\n[1/3] 构建镜像（使用 case 自带 Dockerfile，失败时 LLM 自动修复）...")
                self._build_with_llm_repair()
                print(f"  ✅ 镜像构建完成: {self.final_image_tag}")

            print("\n[2/3] 准备容器 ...")
            container_id = self._start_container()
            print(f"  ✅ 容器就绪: {container_id[:12]}")

            print("\n[3/3] 保存容器信息 ...")
            self._save_container_info(container_id)

            return 0

        except _BuildFailed as e:
            print(f"\n❌ 镜像构建失败（docker 退出码 {e.returncode}）")
            return 1
        except subprocess.CalledProcessError as e:
            print(f"\n❌ 命令执行失败（退出码 {e.returncode}）")
            return 1
        except Exception as e:
            print(f"\n❌ 镜像构建失败: {e}")
            return 1

    def _build_from_dockerfile(self):
        cmd = [
            "docker", "build",
            "-t", self.final_image_tag,
            "-f", str(self.dockerfile),
        ]

        # 清空代理 build-arg，覆盖 Docker Desktop 自动注入的宿主机代理。
        # 镜像构建全程使用国内源（清华 apt/pip、淘宝 npm），无需代理；
        # 而注入的宿主机代理（如局域网 clash）在容器内不可达，会导致构建失败。
        for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                          "https_proxy", "ALL_PROXY", "all_proxy"):
            cmd.extend(["--build-arg", f"{proxy_var}="])

        if self.force_rebuild:
            cmd.append("--no-cache")

        # 构建上下文 = case 目录（Dockerfile 里的 COPY repo/ 等都相对于它）
        cmd.append(str(self.case_path))

        print(f"    执行: {' '.join(cmd)}")
        # 捕获日志：失败时用于喂给 LLM 修复。stdout/stderr 合并，同时透传到终端。
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode != 0:
            raise _BuildFailed(result.returncode, result.stdout or "")

    @staticmethod
    def _is_unfixable_by_llm(build_log: str) -> str:
        """判断构建失败是否属于「改 Dockerfile 内容也修不好」的类型。

        这类错误发生在 docker 解析命令行/引用阶段，尚未进入 Dockerfile 构建，
        重试 LLM 修复只会白白浪费调用。命中则返回原因描述，否则返回空串。
        """
        log = (build_log or "").lower()
        unfixable_signatures = [
            "repository name must be lowercase",   # tag 含大写
            "invalid tag",                          # tag 非法
            "invalid reference format",             # 镜像引用格式非法
            "no such file or directory",            # 构建上下文/Dockerfile 路径错误
        ]
        for sig in unfixable_signatures:
            if sig in log:
                return sig
        return ""

    def _build_with_llm_repair(self):
        """构建镜像；失败时调用 LLM 修复 Dockerfile 并重建，最多重试若干次。

        LLM 修复受硬约束保护：修复后的 Dockerfile 必须仍包含 Claude Code
        安装等关键标记，否则拒绝该次修复、不使用被破坏的版本。
        """
        try:
            self._build_from_dockerfile()
            return
        except _BuildFailed as first_fail:
            build_log = first_fail.log
            reason = self._is_unfixable_by_llm(build_log)
            if reason:
                raise RuntimeError(
                    f"构建失败且无法通过修改 Dockerfile 修复（命中「{reason}」），已跳过 LLM 修复。\n"
                    "  该错误发生在 docker 解析命令行/镜像引用阶段，请检查镜像名、tag、"
                    "构建上下文路径等（例如 tag 是否含大写字母）。"
                )
            print(f"\n⚠️  首次构建失败（退出码 {first_fail.returncode}），尝试用 LLM 修复 Dockerfile ...")

        for attempt in range(1, MAX_LLM_FIX_ATTEMPTS + 1):
            print(f"\n[LLM 修复 {attempt}/{MAX_LLM_FIX_ATTEMPTS}] 分析构建错误并修复 Dockerfile ...")
            if not self._llm_fix_dockerfile(build_log):
                raise RuntimeError("LLM 未能产出合法的修复版 Dockerfile，已终止（保留原 Dockerfile）")
            try:
                self._build_from_dockerfile()
                print(f"  ✅ LLM 修复后构建成功（第 {attempt} 次修复）")
                return
            except _BuildFailed as f:
                build_log = f.log
                reason = self._is_unfixable_by_llm(build_log)
                if reason:
                    raise RuntimeError(
                        f"构建失败且无法通过修改 Dockerfile 修复（命中「{reason}」），已停止后续 LLM 修复。"
                    )
                print(f"  ❌ 修复后仍失败（退出码 {f.returncode}）")

        raise RuntimeError(f"经 {MAX_LLM_FIX_ATTEMPTS} 次 LLM 修复后仍无法构建镜像")

    def _llm_fix_dockerfile(self, build_log: str) -> bool:
        """让 LLM 根据构建日志修复 Dockerfile。

        修复版必须通过关键标记校验（Claude Code 安装等不被删除），
        通过后覆盖写回 case 目录的 Dockerfile 并返回 True；否则返回 False。
        """
        current = self.dockerfile.read_text(encoding="utf-8")

        prompt_file = (
            Path(__file__).parent.parent / "prompts" / "dockerfile_build_fix.txt"
        )
        template = prompt_file.read_text(encoding="utf-8")

        # 只取日志末尾，避免超长；docker 关键错误通常在末尾
        log_tail = build_log[-6000:] if len(build_log) > 6000 else build_log

        # 用 token 替换而非 str.format：Dockerfile / 日志中含大量 ${..} 与 {{..}}
        # 花括号，str.format 会将其误判为占位符而崩溃。
        prompt = (
            template
            .replace("@@DOCKERFILE@@", current)
            .replace("@@CONTEXT_TREE@@", self._context_tree())
            .replace("@@BUILD_LOG@@", log_tail)
        )

        try:
            response = call_llm(prompt, temperature=0.0)
        except Exception as e:
            print(f"    ⚠️  调用 LLM 失败: {e}")
            return False

        fixed = self._extract_dockerfile(response)
        if not fixed:
            print("    ⚠️  LLM 未返回可用的 Dockerfile 内容")
            return False

        missing = [m for m in REQUIRED_DOCKERFILE_MARKERS if m not in fixed]
        if missing:
            print(f"    ⚠️  拒绝修复版：缺失关键标记 {missing}（不会破坏 Claude Code 安装）")
            return False

        self.dockerfile.write_text(fixed, encoding="utf-8")
        print(f"    ✅ 已写入 LLM 修复版 Dockerfile: {self.dockerfile}")
        return True

    @staticmethod
    def _extract_dockerfile(response: str) -> str:
        """从 LLM 响应中提取 Dockerfile 文本（兼容偶发的 ``` 包裹）。"""
        text = response.strip()
        if "```" in text:
            parts = text.split("```")
            # 取最长的代码块内容
            candidates = []
            for i in range(1, len(parts), 2):
                block = parts[i]
                # 去掉可能的语言标识首行（如 dockerfile）
                lines = block.split("\n")
                if lines and lines[0].strip().lower() in ("dockerfile", "docker"):
                    lines = lines[1:]
                candidates.append("\n".join(lines).strip())
            if candidates:
                text = max(candidates, key=len)
        if "FROM " not in text:
            return ""
        return text.strip() + "\n"

    def _context_tree(self) -> str:
        """列出 case 目录下的文件结构（供 LLM 判断 COPY 路径是否存在）。"""
        lines = []
        base = self.case_path
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(base)
            # 跳过 repo 深层内容与噪声目录，保留一层可见性
            parts = rel.parts
            if any(p in (".git", "__pycache__", "node_modules") for p in parts):
                continue
            if parts and parts[0] == "repo" and len(parts) > 2:
                continue
            suffix = "/" if path.is_dir() else ""
            lines.append(f"{rel}{suffix}")
            if len(lines) > 400:
                lines.append("... (truncated)")
                break
        return "\n".join(lines)

    def _start_container(self) -> str:
        """确保容器就绪：已在运行则复用，否则新建（长期运行模式，host 网络以访问宿主机 gateway）"""
        container_name = f"eval-{self.case_path.name}"

        # 未强制重建镜像时，若同名容器已在运行则直接复用，
        # 避免每次跑步骤2都重建容器、丢失容器内已创建的工作空间（candidates/）。
        if not self.force_rebuild:
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                capture_output=True, text=True,
            )
            if state.returncode == 0 and state.stdout.strip() == "true":
                cid = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Id}}", container_name],
                    capture_output=True, text=True,
                ).stdout.strip()
                self._container_reused = True
                print(f"  ♻️  复用已在运行的容器: {container_name} ({cid[:12]})")
                return cid

        # 强制重建 / 容器不存在或已停止：清理同名容器后新建
        self._container_reused = False
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True
        )

        # 启动新容器（host 网络让容器直接访问宿主机网络栈，解决 gateway 连通性）
        # 清空代理环境变量：Docker Desktop 会注入宿主机代理，但 gateway 需直连（与 Dockerfile 中清空代理逻辑一致）
        cmd = [
            "docker", "run",
            "-d",  # 后台运行
            "--name", container_name,
            "--network", "host",  # 共享宿主机网络
            "-e", "HTTP_PROXY=",
            "-e", "HTTPS_PROXY=",
            "-e", "http_proxy=",
            "-e", "https_proxy=",
            "-e", "ALL_PROXY=",
            "-e", "all_proxy=",
            "-e", "NO_PROXY=*",
            "-e", "no_proxy=*",
            "-w", "/workspace",  # 工作目录
            self.final_image_tag,
            "sleep", "infinity"  # 保持运行
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        container_id = result.stdout.strip()

        return container_id

    def _save_container_info(self, container_id: str):
        """保存容器信息供后续步骤使用"""
        container_name = f"eval-{self.case_path.name}"

        # 保存到 case 目录下
        info_file = self.case_path / ".container_info.json"
        info = {
            "container_id": container_id,
            "container_name": container_name,
            "image": self.final_image_tag,
            "case": self.case_path.name
        }

        with open(info_file, "w") as f:
            json.dump(info, f, indent=2)

        print(f"  ✅ 容器信息已保存: {info_file}")

        # 仅当容器被重建（而非复用）时，旧工作空间缓存才失效需清理。
        # 复用容器时缓存与容器仍匹配，工作空间还在，不应删除。
        if not getattr(self, "_container_reused", False):
            workspace_info = self.case_path / ".workspace_info.json"
            if workspace_info.exists():
                workspace_info.unlink()
                print(f"  🧹 容器已重建，清理过期工作空间缓存: {workspace_info.name}")

        print(f"\n进入容器：")
        print(f"  docker exec -it {container_name} bash")

    def _show_image_info(self):
        result = subprocess.run(
            ["docker", "image", "inspect", self.final_image_tag,
             "--format", "{{.Id}}|{{.Size}}|{{.Created}}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("    ⚠️  无法获取镜像信息")
            return

        image_id, size, created = result.stdout.strip().split("|")
        print("\n" + "=" * 80)
        print("镜像信息")
        print("=" * 80)
        print(f"镜像名称: {self.final_image_tag}")
        print(f"镜像 ID : {image_id.replace('sha256:', '')[:12]}")
        print(f"镜像大小: {int(size) / (1024 ** 3):.2f} GB")
        print(f"创建时间: {created}")
        print("\n运行容器：")
        print(f"  docker run --rm -it {self.final_image_tag} bash")


def main():
    parser = argparse.ArgumentParser(
        description="构建评测镜像（直接使用 case 自带 Dockerfile）"
    )
    parser.add_argument("--case", required=True, help="Case 目录路径")
    parser.add_argument("--force-rebuild", action="store_true", help="强制重新构建（--no-cache）")

    args = parser.parse_args()

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"❌ Case 目录不存在: {case_path}")
        return 1

    builder = ImageBuilder(
        case_path=case_path,
        force_rebuild=args.force_rebuild,
    )
    return builder.run()


if __name__ == "__main__":
    sys.exit(main())

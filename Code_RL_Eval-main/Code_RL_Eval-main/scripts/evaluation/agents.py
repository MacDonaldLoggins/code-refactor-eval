"""
Agent 供应商（vendor）分派

根据模型所属的 vendor 决定使用哪个 agent CLI 来执行任务。

目前仅真正支持 claude（Claude Code CLI）；其余 vendor（如 gpt）保留结构，
运行到时会按"暂不支持"跳过并标记，其余 claude 模型正常执行。
"""

from typing import List

# 目前真正支持的供应商
SUPPORTED_VENDORS = {"claude"}

# 模型名前缀 -> vendor 的推断规则
# 用于未显式标注 vendor 的场景（如命令行 --models 覆盖、旧的扁平 models 配置）
_VENDOR_PREFIXES = {
    "claude": "claude",
    "gpt": "gpt",
    "o1": "gpt",
    "o3": "gpt",
    "o4": "gpt",
}


def infer_vendor(model_name: str) -> str:
    """根据模型名推断 vendor（无法识别时返回 'unknown'）"""
    name = (model_name or "").lower()
    for prefix, vendor in _VENDOR_PREFIXES.items():
        if name.startswith(prefix):
            return vendor
    return "unknown"


def is_supported(vendor: str) -> bool:
    """该 vendor 当前是否已支持"""
    return vendor in SUPPORTED_VENDORS


def build_refactor_command(vendor: str, container_name: str, ws_path: str,
                           prompt_ref: str = "@refactor_task.md",
                           session_id: str = None) -> List[str]:
    """构造在容器工作空间里执行重构的 agent 命令（docker exec ...）

    Args:
        vendor: 供应商（claude / gpt / ...）
        container_name: 目标容器名
        ws_path: 工作空间路径（agent 的工作目录为其下的 repo/ 代码目录）
        prompt_ref: 传给 agent 的提示词引用（建议用绝对路径，因为 cwd 在 repo/）
        session_id: 指定 Claude Code 的会话 ID（合法 UUID）。指定后，其原生会话
            轨迹会落到 ~/.claude/projects/<cwd转义>/<session_id>.jsonl，调用方
            据此文件名精确定位并导出修复轨迹，无需依赖 cwd->目录名的转义推导。

    Raises:
        ValueError: vendor 暂不支持
    """
    if vendor == "claude":
        cmd = [
            "docker", "exec",
            "-e", "IS_SANDBOX=1",  # 允许 --dangerously-skip-permissions 在 root 下运行
            "-w", f"{ws_path}/repo",  # 工作目录下移到代码目录 repo/（独立 git 仓库，边界清晰）
            container_name,
            "claude", "--dangerously-skip-permissions",
        ]
        if session_id:
            cmd += ["--session-id", session_id]
        cmd.append(prompt_ref)
        return cmd

    raise ValueError(
        f"vendor '{vendor}' 暂不支持"
        f"（当前仅支持: {', '.join(sorted(SUPPORTED_VENDORS))}）"
    )

"""
提示词加载器：从 prompts/ 目录加载提示词模板
"""
from pathlib import Path


def load_prompt(prompt_name: str) -> str:
    """
    加载提示词模板

    Args:
        prompt_name: 提示词文件名（不含 .txt 后缀）

    Returns:
        提示词模板字符串

    Raises:
        FileNotFoundError: 如果提示词文件不存在
    """
    prompts_dir = Path(__file__).parent.parent / "prompts"
    prompt_path = prompts_dir / f"{prompt_name}.txt"

    if not prompt_path.exists():
        raise FileNotFoundError(f"提示词文件不存在: {prompt_path}")

    with open(prompt_path, 'r', encoding='utf-8') as f:
        return f.read()

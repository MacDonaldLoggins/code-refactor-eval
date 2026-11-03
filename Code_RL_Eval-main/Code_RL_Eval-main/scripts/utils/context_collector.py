"""
代码上下文收集器：从 repo/ 自动收集必要文件
"""
import os
from pathlib import Path
from typing import List, Dict


def collect_directory_tree(repo_path: Path, max_depth: int = 3) -> str:
    """收集目录树结构"""
    lines = []

    def walk_dir(path: Path, prefix: str = "", depth: int = 0):
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            for i, entry in enumerate(entries):
                is_last = i == len(entries) - 1
                current_prefix = "└── " if is_last else "├── "
                lines.append(f"{prefix}{current_prefix}{entry.name}")

                if entry.is_dir() and not entry.name.startswith('.'):
                    next_prefix = prefix + ("    " if is_last else "│   ")
                    walk_dir(entry, next_prefix, depth + 1)
        except PermissionError:
            pass

    lines.append(repo_path.name + "/")
    walk_dir(repo_path)
    return "\n".join(lines)


def find_config_files(repo_path: Path) -> List[Path]:
    """查找项目配置文件"""
    config_patterns = [
        "README.md", "readme.md", "README",
        "pyproject.toml", "setup.py", "requirements.txt",
        "package.json", "package-lock.json",
        "Cargo.toml", "Cargo.lock",
        "go.mod", "go.sum",
        "pom.xml", "build.gradle",
        "Makefile", "CMakeLists.txt"
    ]

    found = []
    for pattern in config_patterns:
        path = repo_path / pattern
        if path.exists() and path.is_file():
            found.append(path)

    return found


def read_file_safe(path: Path, max_lines: int = 500) -> str:
    """安全读取文件，限制行数"""
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    lines.append(f"\n... (truncated at {max_lines} lines)")
                    break
                lines.append(line.rstrip())
            return "\n".join(lines)
    except Exception as e:
        return f"[Error reading file: {e}]"


def collect_repo_context(task_dir: Path) -> Dict[str, str]:
    """
    收集任务目录的代码上下文

    Returns:
        字典包含: directory_tree, refactor_md, config_files
    """
    repo_path = task_dir / "repo"
    refactor_path = task_dir / "refactor.md"

    context = {}

    # 目录树
    if repo_path.exists():
        context["directory_tree"] = collect_directory_tree(repo_path)

    # refactor.md
    if refactor_path.exists():
        context["refactor_md"] = read_file_safe(refactor_path)

    # 配置文件
    config_files = find_config_files(repo_path) if repo_path.exists() else []
    context["config_files"] = {}
    for cfg_path in config_files:
        rel_path = cfg_path.relative_to(repo_path)
        context["config_files"][str(rel_path)] = read_file_safe(cfg_path, max_lines=200)

    return context

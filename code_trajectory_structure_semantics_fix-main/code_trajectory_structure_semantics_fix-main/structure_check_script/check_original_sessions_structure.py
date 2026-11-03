#!/usr/bin/env python3
"""检查 TASK 项目的 original_sessions 目录结构是否正确。

正确的目录结构：
  original_sessions/
    ├── 8c7c8ec8-dbc0-4db2-b5f0-88c9a398cb12/     # UUID 格式的子文件夹
    │   └── subagents/
    │       └── xxx.jsonl
    ├── 另一个UUID/
    └── xxx.jsonl

错误的目录结构：
  original_sessions/
    ├── subagents/          # 非 UUID 格式，应该在 UUID 文件夹内部
    ├── tool-results/       # 非 UUID 格式
    └── xxx.jsonl

检查规则：
  1. original_sessions 直接子文件夹必须是 UUID 格式
  2. 非 UUID 格式的文件夹（如 subagents, tool-results）只能出现在 UUID 文件夹内部
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_NAME = "check_original_sessions_structure.json"
REPORT_TYPE = "check_original_sessions_structure_report"
ORIGINAL_SESSIONS_DIR = "original_sessions"

# UUID 格式正则：8-4-4-4-12 的十六进制字符
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# 已知的非 UUID 子文件夹名称（这些不应该直接出现在 original_sessions 下）
INVALID_DIRECT_SUBFOLDERS = {"subagents", "tool-results", "tools", "results", "converted_sessions", "convert-sessions"}


@dataclass
class StructureIssue:
    """目录结构问题"""
    folder_name: str
    folder_path: str
    reason: str
    expected: str


@dataclass
class OriginalSessionsResult:
    """单个项目的检查结果"""
    project_id: str
    original_sessions_path: str
    exists: bool = True
    total_subfolders: int = 0
    valid_subfolders: int = 0
    invalid_subfolders: int = 0
    issues: list[StructureIssue] = field(default_factory=list)
    error: str = ""

    @property
    def all_pass(self) -> bool:
        return self.exists and self.invalid_subfolders == 0 and not self.error


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def is_uuid_format(name: str) -> bool:
    """检查文件夹名称是否是 UUID 格式"""
    return bool(UUID_RE.match(name))


def check_original_sessions_structure(project_dir: Path) -> OriginalSessionsResult:
    """检查单个项目的 original_sessions 目录结构"""
    original_sessions_path = project_dir / ORIGINAL_SESSIONS_DIR
    result = OriginalSessionsResult(
        project_id=project_dir.name,
        original_sessions_path=str(original_sessions_path)
    )

    # 检查目录是否存在
    if not original_sessions_path.exists():
        result.exists = False
        result.error = f"{ORIGINAL_SESSIONS_DIR} 目录不存在"
        return result

    if not original_sessions_path.is_dir():
        result.exists = False
        result.error = f"{ORIGINAL_SESSIONS_DIR} 不是目录"
        return result

    # 遍历 original_sessions 的直接子项
    for item in original_sessions_path.iterdir():
        # 只检查文件夹，跳过文件（如 .jsonl 文件）
        if not item.is_dir():
            continue

        result.total_subfolders += 1
        folder_name = item.name

        # 跳过隐藏文件夹
        if folder_name.startswith("."):
            continue

        # 检查是否是 UUID 格式
        if is_uuid_format(folder_name):
            result.valid_subfolders += 1
        else:
            result.invalid_subfolders += 1
            
            # 判断具体原因
            if folder_name.lower() in INVALID_DIRECT_SUBFOLDERS:
                reason = f"'{folder_name}' 不应该直接出现在 {ORIGINAL_SESSIONS_DIR} 下，应该在 UUID 文件夹内部"
            else:
                reason = f"'{folder_name}' 不是 UUID 格式"
            
            result.issues.append(StructureIssue(
                folder_name=folder_name,
                folder_path=str(item),
                reason=reason,
                expected="UUID 格式 (如: 8c7c8ec8-dbc0-4db2-b5f0-88c9a398cb12)"
            ))

    return result


def build_json_report(project_dir: Path, result: OriginalSessionsResult) -> dict[str, Any]:
    """构建 JSON 报告"""
    tasks: list[dict[str, Any]] = []
    
    # 如果目录不存在，添加一个错误任务
    if not result.exists:
        tasks.append({
            "检查项id": "original_sessions_existence",
            "产物id": result.project_id,
            "文件id": ORIGINAL_SESSIONS_DIR,
            "行号": 0,
            "原因": result.error,
            "content 原文": ""
        })
    
    # 添加每个结构问题
    for issue in result.issues:
        tasks.append({
            "检查项id": "original_sessions_structure",
            "产物id": result.project_id,
            "文件id": issue.folder_path,
            "行号": 0,
            "原因": issue.reason,
            "content 原文": f"期望格式: {issue.expected}"
        })

    return {
        "report_type": REPORT_TYPE,
        "project_id": result.project_id,
        "source_root": str(project_dir),
        "generated_at": generated_at_text(),
        "summary": {
            "exists": result.exists,
            "total_subfolders": result.total_subfolders,
            "valid_subfolders": result.valid_subfolders,
            "invalid_subfolders": result.invalid_subfolders,
            "failed": len(tasks),
            "passed": 1 if result.all_pass else 0,
            "status": "PASS" if result.all_pass else "FAIL"
        },
        "tasks": tasks
    }


def run(
    find_path: Path | str,
    session_json_path: Path | str = "sessions",
    output_dir: Path | str | None = None,
) -> Path:
    """
    check_pipeline.py 调用的标准入口。
    检查单个项目的 original_sessions 目录结构。
    """
    project_dir = Path(find_path).expanduser().resolve()
    
    # 构造报告输出路径
    report_output_dir = Path(output_dir).expanduser().resolve() if output_dir else project_dir
    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_output_dir / REPORT_NAME

    # 执行检查
    result = check_original_sessions_structure(project_dir)
    
    # 生成报告
    report_payload = build_json_report(project_dir, result)
    report_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查 TASK 项目的 original_sessions 目录结构是否正确"
    )
    parser.add_argument(
        "-r", "--root",
        dest="find_path",
        type=Path,
        required=True,
        help="TASK 项目目录路径"
    )
    parser.add_argument(
        "-o", "--output",
        dest="output_dir",
        type=Path,
        default=None,
        help=f"报告输出目录，默认为项目目录。生成 {REPORT_NAME}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_dir = Path(args.find_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else project_dir
    
    print(f"检查 original_sessions 目录结构: {project_dir.name}")
    
    result = check_original_sessions_structure(project_dir)
    
    # 输出结果
    if not result.exists:
        print(f"  错误: {result.error}")
    elif result.all_pass:
        print(f"  通过: {result.valid_subfolders} 个有效子文件夹")
    else:
        print(f"  失败: {result.invalid_subfolders} 个无效子文件夹")
        for issue in result.issues:
            print(f"    - {issue.folder_name}: {issue.reason}")
    
    # 生成报告
    report_path = run(project_dir, output_dir=output_dir)
    print(f"  报告: {report_path}")
    
    return 0 if result.all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
第一步：克隆代码仓库快照

从 refactor_standard_20260427_061924_600items.json 读取所有任务，
为每个任务下载 snapshot_download_url，解压到 manual_tasks/<owner>__<repo>__<short_sha>__<number>/repo/
同时保存 refactor.md (body) 和 metadata.json (基础信息)。

用法：
    python scripts/01_clone_repos.py --number 57
    python scripts/01_clone_repos.py --numbers 57,703,123
"""

import json
import os
import sys
import argparse
import urllib.request
import tarfile
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any
from urllib.parse import urlparse


def load_json_data(json_path: str) -> list:
    """加载 JSON 数据（兼容 JSONL、JSON 数组和单个对象）"""
    with open(json_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()
    # JSONL：多行，每行一个 JSON 对象
    if '\n' in content and not content.startswith('['):
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    data = json.loads(content)
    if isinstance(data, dict):
        return [data]
    return data


def field(item: Dict[str, Any], *keys, default=None):
    """按顺序取第一个存在且非空的字段值，兼容新旧字段命名（delivery 格式优先）"""
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return default


def item_number(item: Dict[str, Any]):
    """获取任务编号，兼容 pr_number（delivery）和 number（旧格式）"""
    return field(item, 'pr_number', 'number')


def extract_short_sha(commit_id: str, length: int = 7) -> str:
    """提取短 commit SHA"""
    return commit_id[:length] if commit_id else "unknown"


def build_task_dir_name(item: Dict[str, Any]) -> str:
    """构建任务目录名: <owner>__<repo>__<short_sha>__<number>"""
    full_name = item.get('full_name', 'unknown/unknown')
    owner, repo = full_name.split('/', 1) if '/' in full_name else ('unknown', 'unknown')
    short_sha = extract_short_sha(field(item, 'before_commit_sha', 'commit_id', default=''))
    number = item_number(item) or 0
    return f"{owner}__{repo}__{short_sha}__{number}"


def download_snapshot(url: str, dest_path: str) -> bool:
    """下载快照 tar.gz 到指定路径"""
    try:
        print(f"  下载中... {url[:80]}...")
        with urllib.request.urlopen(url, timeout=60) as response:
            with open(dest_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
        print(f"  下载完成: {os.path.getsize(dest_path)} bytes")
        return True
    except Exception as e:
        print(f"  ❌ 下载失败: {e}")
        return False


def extract_tarball(tar_path: str, extract_to: str) -> bool:
    """解压 tar.gz，将顶层目录内容移动到 extract_to"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"  解压到临时目录...")
            with tarfile.open(tar_path, 'r:gz') as tar:
                tar.extractall(path=tmpdir)

            # 快照通常是 <owner>-<repo>-<short_sha>/ 单一顶层目录
            entries = os.listdir(tmpdir)
            if len(entries) != 1:
                print(f"  ⚠️  预期单一顶层目录，实际有 {len(entries)} 个条目")
                # 尝试继续，直接移动所有内容
                for entry in entries:
                    src = os.path.join(tmpdir, entry)
                    dst = os.path.join(extract_to, entry)
                    shutil.move(src, dst)
            else:
                # 移动顶层目录内的所有内容到 extract_to
                top_dir = os.path.join(tmpdir, entries[0])
                if os.path.isdir(top_dir):
                    for item in os.listdir(top_dir):
                        src = os.path.join(top_dir, item)
                        dst = os.path.join(extract_to, item)
                        shutil.move(src, dst)
                else:
                    # 如果顶层不是目录，直接移动
                    shutil.move(top_dir, os.path.join(extract_to, entries[0]))

        print(f"  ✓ 解压完成")
        return True
    except Exception as e:
        print(f"  ❌ 解压失败: {e}")
        return False


def save_refactor_md(task_dir: Path, body: str):
    """保存 refactor.md"""
    refactor_path = task_dir / "refactor.md"
    with open(refactor_path, 'w', encoding='utf-8') as f:
        f.write(body)
    print(f"  ✓ 保存 refactor.md")


def save_source_item(task_dir: Path, item: Dict[str, Any]):
    """保存原始数据源 item 到 source_item.json，供后续步骤读取确定性字段（commit SHA、url 等）。

    第 2 步用 LLM 生成 metadata.json 时会覆盖第 1 步的文件，且 LLM 无法得知 commit SHA，
    因此这里单独持久化原始 item 作为权威数据源。
    """
    source_path = task_dir / "source_item.json"
    with open(source_path, 'w', encoding='utf-8') as f:
        json.dump(item, f, indent=2, ensure_ascii=False)
    print(f"  ✓ 保存 source_item.json")


def save_metadata_json(task_dir: Path, item: Dict[str, Any]):
    """保存 metadata.json (与真实 PR case 格式一致)"""
    metadata = {
        "number": item_number(item),
        "title": field(item, 'pr_title', 'title'),
        "full_name": item.get("full_name"),
        "url": field(item, 'pr_url', 'url'),
        "repository_url": item.get("repository_url"),
        # base commit：改动前快照对应的 commit
        "commit_id": field(item, 'before_commit_sha', 'commit_id'),
        # PR 的 head commit（用于第3步生成/校验 patch）
        "pr_commit_sha": field(item, 'pr_head_sha', 'pr_commit_sha'),
        "merge_commit_sha": field(item, 'merge_commit_sha'),
        "refactor_type": field(item, 'refactor_type', 'scenario'),
        "language": field(item, 'diff_primary_language', 'language'),
        "merged_at": field(item, 'pr_merged_at', 'merged_at'),
        "snapshot_download_url": field(item, 'before_download_url', 'snapshot_download_url'),
        # PR diff bundle（第3步优先用其中的 raw.diff 作为参考 patch）
        "diff_download_url": field(item, 'diff_download_url'),
    }
    metadata_path = task_dir / "metadata.json"
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print(f"  ✓ 保存 metadata.json")


def process_item(item: Dict[str, Any], base_dir: Path) -> bool:
    """处理单个任务：下载、解压、保存元数据"""
    task_name = build_task_dir_name(item)
    task_dir = base_dir / task_name
    repo_dir = task_dir / "repo"

    print(f"\n处理 [{item_number(item)}] {item.get('full_name')} - {task_name}")

    # 检查是否已存在
    if repo_dir.exists():
        print(f"  ⏭️  已存在，跳过")
        return True

    # 创建任务目录
    task_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(exist_ok=True)

    # 下载快照（改动前的仓库状态）
    download_url = field(item, 'before_download_url', 'snapshot_download_url')
    if not download_url:
        print(f"  ❌ 缺少 snapshot 下载地址 (before_download_url / snapshot_download_url)")
        return False

    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp_file:
        tmp_path = tmp_file.name

    try:
        # 下载
        if not download_snapshot(download_url, tmp_path):
            return False

        # 解压到 repo/
        if not extract_tarball(tmp_path, str(repo_dir)):
            return False

        # 保存 refactor.md 和 metadata.json
        save_refactor_md(task_dir, field(item, 'pr_body', 'body', default=''))
        save_metadata_json(task_dir, item)
        save_source_item(task_dir, item)

        print(f"  ✅ 完成: {task_name}")
        return True

    finally:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def main():
    parser = argparse.ArgumentParser(description='克隆 refactor 任务的代码仓库快照')
    parser.add_argument('--number', type=int, help='下载指定 number 的任务')
    parser.add_argument('--numbers', type=str, help='下载多个 number，逗号分隔，如: 57,703,123')
    parser.add_argument('--source', type=str,
                        default='refactor_standard_20260427_061924_600items.json',
                        help='数据源 JSON 文件路径（默认: refactor_standard_20260427_061924_600items.json）')

    args = parser.parse_args()

    # 必须指定 number 或 numbers
    if args.number is None and args.numbers is None:
        print(f"❌ 必须指定 --number 或 --numbers 参数")
        parser.print_help()
        sys.exit(1)

    # 加载数据
    json_path = Path(args.source)
    if not json_path.exists():
        print(f"❌ JSON 文件不存在: {json_path}")
        sys.exit(1)

    print(f"加载数据: {json_path}")
    data = load_json_data(str(json_path))
    print(f"共 {len(data)} 个任务")

    # 收集目标 numbers
    target_numbers = set()
    if args.number is not None:
        target_numbers.add(args.number)
    if args.numbers:
        target_numbers.update(int(n.strip()) for n in args.numbers.split(','))

    subset = [item for item in data if item_number(item) in target_numbers]
    print(f"筛选 number={sorted(target_numbers)}，找到 {len(subset)} 个任务\n")

    if len(subset) == 0:
        print(f"❌ 未找到匹配的任务")
        sys.exit(1)

    # 创建输出目录
    output_dir = Path('manual_tasks')
    output_dir.mkdir(exist_ok=True)

    # 处理每个任务
    success_count = 0
    failed_count = 0

    for item in subset:
        try:
            if process_item(item, output_dir):
                success_count += 1
            else:
                failed_count += 1
        except KeyboardInterrupt:
            print("\n\n⚠️  用户中断")
            break
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            failed_count += 1

    # 汇总
    print(f"\n{'='*60}")
    print(f"完成: {success_count} 个成功, {failed_count} 个失败")
    print(f"输出目录: {output_dir.absolute()}")


if __name__ == '__main__':
    main()

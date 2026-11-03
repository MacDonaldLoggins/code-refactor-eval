#!/usr/bin/env python3
"""从 convert 源恢复包含畸形 message 节点的 session 文件。

用法：
  python fix_malformed_from_source.py <交付根目录> --source <源根目录>

只修复检测到包含非 dict 节点、空 dict 节点或缺 role 字段的文件。
从源的 convert-sessions 或 sessions 查找对应文件并整文件覆盖。
"""
import argparse
import json
import shutil
from pathlib import Path


def is_malformed(messages):
    """检查 messages 数组是否包含畸形节点。"""
    if not isinstance(messages, list):
        return True
    for msg in messages:
        if not isinstance(msg, dict):
            return True
        if not msg:  # 空 dict
            return True
        if not msg.get('role'):
            return True
    return False


def find_source_file(deliv_file, source_root, source_state):
    """查找源文件（逻辑类似 fix_broken_json_from_source.py）。"""
    # 简化版：直接根据相对路径查找
    rel = deliv_file.relative_to(deliv_file.parents[3])  # 去掉 TASK-* 之前的部分

    # 尝试 convert-sessions
    if source_state in ("auto", "convert"):
        convert_path = source_root / rel.parts[0] / "convert-sessions" / "/".join(rel.parts[1:])
        if convert_path.is_file():
            return convert_path

    # 尝试 sessions
    if source_state in ("auto", "sessions"):
        sessions_path = source_root / rel
        if sessions_path.is_file():
            return sessions_path

    return None


def process_file(file_path, source_root, source_state, apply):
    """处理单个文件。"""
    try:
        data = json.loads(file_path.read_text(encoding='utf-8-sig'))
        messages = data.get('messages', [])

        if not is_malformed(messages):
            return None  # 文件正常，跳过

        # 统计畸形节点
        bad_indices = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or not msg or not msg.get('role'):
                bad_indices.append(i)

        # 查找源文件
        source_file = find_source_file(file_path, source_root, source_state)
        if not source_file:
            return {
                "file": str(file_path),
                "status": "no_source",
                "malformed_count": len(bad_indices),
                "malformed_indices": bad_indices[:10],
            }

        # 验证源文件是否干净
        source_data = json.loads(source_file.read_text(encoding='utf-8-sig'))
        source_messages = source_data.get('messages', [])
        if is_malformed(source_messages):
            return {
                "file": str(file_path),
                "status": "source_also_malformed",
                "malformed_count": len(bad_indices),
            }

        # 应用修复
        if apply:
            shutil.copy2(source_file, file_path)

        return {
            "file": str(file_path),
            "status": "fixed" if apply else "can_fix",
            "malformed_count": len(bad_indices),
            "malformed_indices": bad_indices[:10],
            "source_file": str(source_file),
            "applied": apply,
        }

    except Exception as e:
        return {
            "file": str(file_path),
            "status": "error",
            "error": str(e),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deliv_root", type=Path, help="交付版根目录")
    parser.add_argument("--source", type=Path, required=True, help="源数据根目录")
    parser.add_argument("--source-state", choices=("auto", "convert", "sessions"),
                       default="convert", help="源查找范围")
    parser.add_argument("-path", "--session-path", default="sessions",
                       help="session 目录名")
    parser.add_argument("--apply", action="store_true", help="应用修复")
    parser.add_argument("-o", "--output", type=Path,
                       default=Path("fix_malformed_report.json"), help="报告路径")
    args = parser.parse_args()

    results = []
    deliv_root = args.deliv_root.resolve()
    source_root = args.source.resolve()

    # 扫描所有 session 文件
    for task_dir in deliv_root.glob("TASK-*"):
        sessions_dir = task_dir / args.session_path
        if not sessions_dir.is_dir():
            continue

        for json_file in sessions_dir.rglob("*.json"):
            if json_file.name.startswith(".") or json_file.name.endswith(".meta.json"):
                continue

            result = process_file(json_file, source_root, args.source_state, args.apply)
            if result:
                results.append(result)
                status = result['status']
                count = result.get('malformed_count', 0)
                applied = result.get('applied', False)
                print(f"[{status}] malformed={count} applied={applied} {json_file.relative_to(deliv_root)}")

    summary = {
        "scanned_malformed": len(results),
        "fixed": sum(1 for r in results if r['status'] in ('fixed', 'can_fix')),
        "applied": sum(1 for r in results if r.get('applied')),
        "no_source": sum(1 for r in results if r['status'] == 'no_source'),
        "source_also_malformed": sum(1 for r in results if r['status'] == 'source_also_malformed'),
    }

    report = {
        "report_type": "fix_malformed_report",
        "source_root": str(source_root),
        "source_state": args.source_state,
        "apply": args.apply,
        "summary": summary,
        "results": results,
    }

    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\nreport: {args.output}  summary={summary}")

    if not args.apply:
        print("dry-run（未写盘）；加 --apply 才写回。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

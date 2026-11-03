#!/usr/bin/env python3
"""
修复 mojibake（编码乱码）脚本 v3
================================
原理: 原本 UTF-8 编码的文件被错误地当做 GB18030/GBK 读取，
      导致 CJK 乱码字符。通过以下步骤恢复整个 JSON 树中的字符串：
      1. GB18030 编码回退 → UTF-8 解码（主方法）
      2. 直接映射表（针对无法通过编码回退恢复的字符）
      3. 如果仍无法恢复，删除剩余 CJK 字符

用法:
  python fix_mojibake.py -r <根目录> -i sessions -o <报告路径>

  -o 可以是文件路径或目录路径，目录时默认使用 fix_mojibake_report.json

示例:
  python fix_mojibake.py -r /home/lcs/workspace/data/7-7 -i sessions -o /home/lcs/workspace/data/7-7/report.json
  python fix_mojibake.py -r /home/lcs/workspace/data/7-7 -i sessions -o /home/lcs/workspace/data/7-7-report
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import re
REPORT_NAME = "fix_mojibake_report.json"
REPORT_TYPE = "fix_mojibake_report"
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}


def is_skipped_json_file(filepath: str) -> bool:
    parts = set(os.path.normpath(filepath).split(os.sep))
    name = os.path.basename(filepath)
    return (
        bool(parts & SKIPPED_SESSION_DIR_NAMES)
        or name.startswith(".")
        or name.endswith(".meta.json")
    )


FFFD = '�'  # Unicode replacement character

# ============================================================
# 直接映射表：已知的 mojibake → 原始字符
# 这些是 GB18030→UTF-8 编码回退无法处理的情况
# ============================================================
DIRECT_MAP = {
    '鈥': '',   # 鈥 → em dash / curly quote（缺少第3字节，无法确定具体是哪个）
    '鈫': '→',  # 鈫 → → (right arrow, 最可能)
    '鈹': '├',  # 鈹 → ├ (box drawing tee, 最可能)
    '鉁': '',   # 鉁 → emoji/bullet（不确定具体是哪个）
    '閳': '',   # 閳 → 未知
    # 以下是 UTF-8 续字节被误读为 GB18030 的情况
    '扐': '',   # 扐 → 0x92 → UTF-8 续字节（不确定原字符）
    '払': '',   # 払 → 0x93
    '扗': '',   # 扗 → 0x94
    '扝': '',   # 扝 → 0x96
    '扤': '',   # 扤 → 0x99
    '扨': '',   # 扨 → 0x9A
    '扴': '',   # 扴 → 0x9E
    '抯': '',   # 抯 → 0xA4
    '搢': '',   # 搢 → 0xB8
    '擨': '',   # 擨 → 0xC3
    '攃': '',   # 攃 → 0xC8
    '攔': '',   # 攔 → 0xCE
    '攕': '',   # 攕 → 0xCF
    '攖': '',   # 攖 → 0xD0
    '攢': '',   # 攢 → 0xDE
    '斺': '',   # 斺 → 0xFA
    '梶': '',   # 梶 → 0xBC
    '溾': '',   # 溾 → 0xFE
}

# 对于可以从上下文恢复的，这里映射后续的连续字符
# 这些是当 mojibake 字符后跟特定字符时可以恢复的组合
PAIR_MAP = {
    # 鈥檚 → 's, 鈥檙 → —r, 鈥檛 → —t, 鈥橔 → …T, 鈥橔 → …The
    '鈥檚': "'s",
    '鈥檙': '—r',  # —r
    '鈥檛': '—t',  # —t
    '鈥橔': '…T',  # …T
    '鈥溾': '“',   # "
    '鈥斺': '“',   # "
    '鈥攏': '—n',  # —n
    '鈥攔': '—r',  # —r
    '鈹溾攢': '├─',  # ├─
    '鈹斺攢': '└─',  # └─
}

# 单个字符编码回退测试结果：可恢复的映射
ENCODING_MAP = {
    '帽': 'ñ',   # 帽 → ñ (from c3b1)
    '脳': '×',   # 脳 → × (from c397)
}


def recover_text(text: str) -> str:
    """通过多阶段方法恢复 mojibake 文本"""
    if not text:
        return text
    # 检查是否包含 CJK 字符（mojibake 的标志）或 U+FFFD（Unicode 替换字符）
    has_cjk = any('一' <= ch <= '鿿' for ch in text)
    has_fffd = FFFD in text
    if not has_cjk and not has_fffd:
        return text
    # 仅含 U+FFFD 不含 CJK 时，跳过编码回退，尝试恢复/清除
    if not has_cjk and has_fffd:
        # 数字范围 → en dash: "60s�?70s" → "60s–70s", "20�?0 min" → "20–0 min"
        text = re.sub(r'(\d\w*)\s*�\?\s*(\d)', r'\1–\2', text)
        # 其余 �? → em dash: "routes �?read" → "routes —read"
        text = text.replace(FFFD + '?', '—')
        # 剩余的单独 U+FFFD 删除
        return text.replace(FFFD, '')

    # 阶段 1：尝试成对映射（优先于单字符映射）
    for pair, replacement in PAIR_MAP.items():
        text = text.replace(pair, replacement)

    # 阶段 2：单字符编码映射（已知可通过 gb18030 恢复的）
    for char, replacement in ENCODING_MAP.items():
        text = text.replace(char, replacement)

    # 阶段 3：GB18030 编码回退
    try:
        raw_bytes = text.encode('gb18030', errors='replace')
        recovered = raw_bytes.decode('utf-8', errors='replace')
    except Exception:
        recovered = text

    # 移除无法恢复的 U+FFFD
    if FFFD in recovered:
        recovered = recovered.replace(FFFD, '')

    # 阶段 4：对剩余 CJK 应用直接映射，无法映射则删除
    result = []
    for ch in recovered:
        if '一' <= ch <= '鿿':
            if ch in DIRECT_MAP:
                result.append(DIRECT_MAP[ch])
            else:
                # 无法映射，删除
                pass
        else:
            result.append(ch)
    recovered = ''.join(result)

    return recovered


def recover_json_value(value):
    """递归恢复 JSON 值中的所有字符串 mojibake。"""
    if isinstance(value, str):
        return recover_text(value)
    if isinstance(value, list):
        return [recover_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: recover_json_value(item) for key, item in value.items()}
    return value


def fix_session_file(filepath: str) -> dict:
    """修复单个 session 文件中的 mojibake"""
    if not os.path.exists(filepath):
        return {'file': filepath, 'status': 'NOT_FOUND', 'fixed': 0}

    with open(filepath, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            return {'file': filepath, 'status': f'JSON_ERROR: {e}', 'fixed': 0}

    original = json.dumps(data, ensure_ascii=False)
    cjk_bf = sum(1 for c in original if '一' <= c <= '鿿')

    # 递归修复整个 JSON 树，而不是只盯 messages/tools。
    fixed_data = recover_json_value(data)

    after = json.dumps(fixed_data, ensure_ascii=False)
    cjk_af = sum(1 for c in after if '一' <= c <= '鿿')
    changed = after != original

    if changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(json.dumps(fixed_data, ensure_ascii=False, indent=2) + '\n')

    return {
        'file': filepath,
        'status': 'OK' if not changed else 'FIXED',
        'fixed': 1 if changed else 0,
        'cjk_before': cjk_bf,
        'cjk_after': cjk_af,
    }


def fix_project(project_sr: str, project_id: str, session_path: str = 'sessions') -> dict:
    """修复项目中所有可能包含 mojibake 的 session 文件"""
    result = {
        'project': project_id,
        'files_fixed': 0,
        'files_total': 0,
        'cjk_removed': 0,
    }

    sessions_dir = os.path.join(project_sr, session_path)
    if not os.path.isdir(sessions_dir):
        return result

    for root_dir, _dirs, files in os.walk(sessions_dir):
        for filename in sorted(files):
            if not filename.endswith('.json'):
                continue
            filepath = os.path.join(root_dir, filename)
            if is_skipped_json_file(filepath):
                continue
            result['files_total'] += 1
            fr = fix_session_file(filepath)
            if fr.get('status') == 'FIXED':
                result['files_fixed'] += 1
                result['cjk_removed'] += fr['cjk_before'] - fr['cjk_after']

    return result


def build_report(project_results: list[dict]) -> dict:
    """构建与 clean_empty_assistant.py 一致格式的报告。"""
    items: list[dict] = []
    for pr in project_results:
        pid = pr.get('project', 'unknown')
        files_fixed = pr['files_fixed']
        files_total = pr['files_total']
        cjk_removed = pr['cjk_removed']

        if files_fixed:
            items.append({
                "项目": pid,
                "修改前": f"{files_total} 个文件，包含 {cjk_removed} 个 CJK 乱码",
                "修改后": f"{files_total} 个文件，已清除 CJK 乱码",
                "修复文件数": str(files_fixed),
                "移除乱码数": str(cjk_removed),
            })
    return {REPORT_TYPE: items}


def fix_mojibake(root_dir: Path, session_path: str, report_path: Path | None = None) -> Path:
    """扫描 root_dir 下所有 TASK-* 项目，修复 mojibake 并写报告。"""
    root_dir = root_dir.expanduser().resolve()

    # 收集项目目录
    if os.path.isdir(os.path.join(root_dir, session_path)):
        project_dirs = [root_dir]
    else:
        project_dirs = sorted(
            [os.path.join(root_dir, d) for d in os.listdir(root_dir)
             if d.startswith('TASK-') and os.path.isdir(os.path.join(root_dir, d))]
        ) or [root_dir]

    results: list[dict] = []
    total_fixed = 0
    total_files = 0
    total_cjk_removed = 0

    for pd in project_dirs:
        project_id = os.path.basename(pd)
        pr = fix_project(pd, project_id, session_path)
        results.append(pr)
        total_fixed += pr['files_fixed']
        total_files += pr['files_total']
        total_cjk_removed += pr['cjk_removed']

        if pr['files_fixed'] > 0:
            print(
                f'  {project_id}: fixed {pr["files_fixed"]}/{pr["files_total"]} files '
                f'(removed {pr["cjk_removed"]} CJK mojibake chars)'
            )
        else:
            print(f'  {project_id}: no changes ({pr["files_total"]} files checked)')

    # 写总报告
    if report_path is not None:
        report_path_obj = report_path.expanduser().resolve()
        if report_path_obj.is_dir() or report_path_obj.suffix.lower() != '.json':
            report_path_obj = report_path_obj / REPORT_NAME
        report = build_report(results)
        report_path_obj.parent.mkdir(parents=True, exist_ok=True)
        report_path_obj.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        print(f'\nReport: {report_path_obj}')

    print(f'\nDone: checked {total_files} files, fixed {total_fixed}, '
          f'removed {total_cjk_removed} CJK mojibake chars')
    return root_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fix mojibake (encoding corruption) in session JSON files."
    )
    parser.add_argument(
        "-r",
        dest="find_path",
        type=Path,
        required=True,
        help="Root directory containing TASK-* project subdirectories.",
    )
    parser.add_argument(
        "-i",
        dest="session_json_path",
        type=Path,
        required=True,
        help="Session file or session directory path relative to each project directory.",
    )
    parser.add_argument(
        "-o",
        dest="report_path",
        type=Path,
        required=False,
        default=None,
        help=f"Optional: output combined report path (file or directory). Default {REPORT_NAME} under root.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        fix_mojibake(
            root_dir=args.find_path,
            session_path=str(args.session_json_path),
            report_path=args.report_path,
        )
    except Exception as exc:
        print(f"fix_mojibake failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

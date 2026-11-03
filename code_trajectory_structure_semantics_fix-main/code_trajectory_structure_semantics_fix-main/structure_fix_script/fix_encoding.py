#!/usr/bin/env python3
"""Repair JSON file encoding issues.

This step removes UTF-8 BOM markers and rewrites invalid UTF-8 JSON files with
replacement characters. When --session-path is supplied, scanning is limited to
that session directory under the root TASK or each TASK-* child.
"""
from __future__ import annotations

import argparse
import os
import sys


SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}


try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def iter_scan_roots(root: str, session_path: str | None = None) -> list[str]:
    root = os.path.abspath(root)
    if not session_path:
        return [root]

    direct = os.path.join(root, session_path)
    if os.path.isdir(direct):
        return [direct]

    roots: list[str] = []
    for name in sorted(os.listdir(root)):
        if not name.startswith("TASK-"):
            continue
        candidate = os.path.join(root, name, session_path)
        if os.path.isdir(candidate):
            roots.append(candidate)
    return roots


def iter_json_files(root: str, session_path: str | None = None):
    for scan_root in iter_scan_roots(root, session_path):
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in SKIPPED_SESSION_DIR_NAMES and not name.startswith(".")
            ]
            for filename in filenames:
                if filename.startswith(".") or filename.endswith(".meta.json") or not filename.endswith(".json"):
                    continue
                yield os.path.join(dirpath, filename)


def fix_bom(path: str, content: bytes) -> bool:
    if content.startswith(b"\xef\xbb\xbf"):
        with open(path, "wb") as handle:
            handle.write(content[3:])
        return True
    return False


def fix_invalid_utf8(path: str, content: bytes) -> bool:
    try:
        content.decode("utf-8")
        return False
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return True


def scan_only(root: str, session_path: str | None = None) -> tuple[list[str], list[str]]:
    bom_list: list[str] = []
    utf8_list: list[str] = []
    for path in iter_json_files(root, session_path):
        with open(path, "rb") as handle:
            content = handle.read()
        if content.startswith(b"\xef\xbb\xbf"):
            bom_list.append(path)
            continue
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            utf8_list.append(path)
    return bom_list, utf8_list


def fix_all(root: str, session_path: str | None = None) -> tuple[list[str], list[str]]:
    bom_fixed: list[str] = []
    utf8_fixed: list[str] = []
    for path in iter_json_files(root, session_path):
        with open(path, "rb") as handle:
            content = handle.read()
        if fix_bom(path, content):
            bom_fixed.append(path)
            continue
        if fix_invalid_utf8(path, content):
            utf8_fixed.append(path)
    return bom_fixed, utf8_fixed


def print_results(bom_list: list[str], utf8_list: list[str], root: str, *, dry_run: bool = False) -> None:
    mode = "dry-run" if dry_run else "apply"
    print("=" * 60)
    print(f"encoding scan complete ({mode})")
    print("=" * 60)

    if bom_list:
        print(f"\nBOM files ({len(bom_list)}):")
        for path in bom_list:
            print(f"   {path.replace(root, '')}")

    if utf8_list:
        print(f"\ninvalid UTF-8 files ({len(utf8_list)}):")
        for path in utf8_list:
            print(f"   {path.replace(root, '')}")

    if not bom_list and not utf8_list:
        print("\nNo encoding issues found.")
        return

    print(f"\ntotal: {len(bom_list) + len(utf8_list)} file(s)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair JSON BOM and invalid UTF-8 issues.")
    parser.add_argument("root", help="Root directory to scan.")
    parser.add_argument("--dry-run", action="store_true", help="Only scan; do not write files.")
    parser.add_argument(
        "--session-path",
        default=None,
        help="Optional TASK-relative session directory to scan, e.g. data_success.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"error: root not found: {root}", file=sys.stderr)
        return 1

    if args.dry_run:
        bom_list, utf8_list = scan_only(root, args.session_path)
        print_results(bom_list, utf8_list, root, dry_run=True)
    else:
        bom_list, utf8_list = fix_all(root, args.session_path)
        print_results(bom_list, utf8_list, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

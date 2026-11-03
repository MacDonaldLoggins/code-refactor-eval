"""轨迹组发现工具。

用于同时支持两类结构：
1. 单个主 session + 同级 subagent(s) 下的全部 subagent。
2. 多个主 session + 每个主 session 对应的 subagent。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable


JsonFilePredicate = Callable[[Path], bool]


def canonical_agent_id(agent_id: str) -> str:
    value = agent_id.strip()
    return value[6:] if value.startswith("agent-") else value


def dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    deduped: dict[str, Path] = {}
    for path in paths:
        deduped[str(path.resolve()).lower()] = path
    return sorted(deduped.values(), key=lambda item: str(item).lower())


def collect_json_files(root: Path, is_candidate_session_file: JsonFilePredicate) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.rglob("*") if is_candidate_session_file(path)),
        key=lambda path: str(path.relative_to(root)).lower(),
    )


def subagent_file_id(path: Path) -> str:
    return canonical_agent_id(path.stem)


def load_messages(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def referenced_subagent_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for message in load_messages(path):
        metadata = message.get("_metadata")
        metadata_agents = metadata.get("agent") if isinstance(metadata, dict) else None
        if not isinstance(metadata_agents, dict):
            continue
        for value in metadata_agents.values():
            if isinstance(value, str) and value.strip() and value.strip() != "main":
                ids.add(canonical_agent_id(value))
    return ids


def expand_referenced_subagents(main_session_path: Path, candidate_files: list[Path]) -> list[Path]:
    by_id: dict[str, Path] = {subagent_file_id(path): path for path in candidate_files}
    wanted = referenced_subagent_ids(main_session_path)
    selected: list[Path] = []
    seen: set[str] = set()

    while wanted:
        current = wanted.pop()
        if current in seen:
            continue
        seen.add(current)
        subagent_file = by_id.get(current)
        if subagent_file is None:
            continue
        selected.append(subagent_file)
        for child_id in referenced_subagent_ids(subagent_file):
            if child_id not in seen:
                wanted.add(child_id)

    return dedupe_paths(selected)


def sibling_main_session_count(main_session_path: Path, is_main_session_file: JsonFilePredicate) -> int:
    parent = main_session_path.parent
    if not parent.is_dir():
        return 0
    return sum(1 for path in parent.iterdir() if is_main_session_file(path))


def discover_related_subagent_files(
    main_session_path: Path,
    *,
    is_candidate_session_file: JsonFilePredicate,
    is_main_session_file: JsonFilePredicate,
    subagent_dir_names: set[str],
) -> list[Path]:
    parent = main_session_path.parent
    stem = main_session_path.stem

    specific_dirs: list[Path] = []
    stem_dir = parent / stem
    for dir_name in sorted(subagent_dir_names):
        specific_dirs.append(stem_dir / dir_name)
        specific_dirs.append(parent / dir_name / stem)

    specific_files = dedupe_paths(
        path
        for specific_dir in specific_dirs
        for path in collect_json_files(specific_dir, is_candidate_session_file)
    )
    if specific_files:
        return specific_files

    shared_files = dedupe_paths(
        path
        for dir_name in sorted(subagent_dir_names)
        for path in collect_json_files(parent / dir_name, is_candidate_session_file)
    )
    if not shared_files:
        return []

    referenced_files = expand_referenced_subagents(main_session_path, shared_files)
    if referenced_files:
        return referenced_files

    if sibling_main_session_count(main_session_path, is_main_session_file) == 1:
        return shared_files

    return []


def discover_trajectory_group_files(
    main_session_path: Path,
    *,
    is_candidate_session_file: JsonFilePredicate,
    is_main_session_file: JsonFilePredicate,
    subagent_dir_names: set[str],
) -> list[Path]:
    return [
        main_session_path,
        *discover_related_subagent_files(
            main_session_path,
            is_candidate_session_file=is_candidate_session_file,
            is_main_session_file=is_main_session_file,
            subagent_dir_names=subagent_dir_names,
        ),
    ]

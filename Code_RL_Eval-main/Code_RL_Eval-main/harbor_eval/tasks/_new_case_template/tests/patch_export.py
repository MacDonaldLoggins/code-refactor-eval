from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "harbor-refactor-eval/v1"
BASE_COMMIT_FILE = ".base_commit"
PATCH_FILENAME = "refactor.patch"
PATCH_METADATA_FILENAME = "refactor-patch.json"
REPO_EXPORT_DIRNAME = "repo"
REPO_METADATA_FILENAME = "repo.json"
REPO_EXPORT_IGNORED_NAMES = {
    ".git",
    ".base_commit",
    ".cache_ggshield",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
}
REPO_EXPORT_IGNORED_SUFFIXES = (".pyc", ".pyo")


def _base_commit_file() -> str:
    return BASE_COMMIT_FILE


def _base_commit(repo: Path) -> str | None:
    marker = repo / _base_commit_file()
    if not marker.is_file():
        return None
    value = marker.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def _run_git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_index_env(index_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index_file)
    return env


def _repo_export_ignore(_dir: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in REPO_EXPORT_IGNORED_NAMES or name.endswith(
            REPO_EXPORT_IGNORED_SUFFIXES
        ):
            ignored.add(name)
    return ignored


def _repo_export_stats(repo_export_path: Path) -> tuple[int, int]:
    file_count = 0
    byte_size = 0
    for path in repo_export_path.rglob("*"):
        try:
            if not path.is_file():
                continue
            file_count += 1
            byte_size += path.stat().st_size
        except OSError:
            continue
    return file_count, byte_size


def export_repo(repo: Path, logs: Path) -> dict[str, Any]:
    repo = repo.resolve()
    logs = logs.resolve()

    repo_export_path = logs / REPO_EXPORT_DIRNAME
    metadata_path = logs / REPO_METADATA_FILENAME
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo_path": str(repo_export_path),
        "metadata_path": str(metadata_path),
        "base_commit_file": _base_commit_file(),
    }

    try:
        logs.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(repo_export_path, ignore_errors=True)

        if not repo.is_dir():
            metadata.update(
                status="missing_repo",
                error="candidate repo directory is missing",
            )
            return metadata

        shutil.copytree(
            repo,
            repo_export_path,
            ignore=_repo_export_ignore,
            symlinks=True,
        )
        file_count, byte_size = _repo_export_stats(repo_export_path)
        metadata.update(
            status="ok",
            file_count=file_count,
            repo_bytes=byte_size,
        )
    except Exception as exc:
        shutil.rmtree(repo_export_path, ignore_errors=True)
        metadata.update(status="error", error=str(exc))
    finally:
        try:
            _write_json(metadata_path, metadata)
        except Exception:
            pass
    return metadata


def export_patch(repo: Path, logs: Path) -> dict[str, Any]:
    repo = repo.resolve()
    logs = logs.resolve()

    patch_path = logs / PATCH_FILENAME
    metadata_path = logs / PATCH_METADATA_FILENAME
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "patch_path": str(patch_path),
        "metadata_path": str(metadata_path),
        "base_commit_file": _base_commit_file(),
    }

    try:
        logs.mkdir(parents=True, exist_ok=True)
        patch_path.unlink(missing_ok=True)

        base = _base_commit(repo)
        if not base:
            metadata.update(
                status="missing_base_commit",
                error="base commit marker is missing",
            )
            return metadata

        valid = _run_git(repo, "cat-file", "-e", f"{base}^{{commit}}")
        if valid.returncode != 0:
            metadata.update(
                status="invalid_base_commit",
                base_commit=base,
                error="base commit is not available in candidate git history",
            )
            return metadata

        with tempfile.TemporaryDirectory(prefix="patch-export-", dir=logs) as temp_dir:
            index_file = Path(temp_dir) / "index"
            env = _build_index_env(index_file)
            read_tree = _run_git(repo, "read-tree", base, env=env)
            if read_tree.returncode != 0:
                metadata.update(
                    status="error",
                    base_commit=base,
                    error=read_tree.stderr.strip() or "git read-tree failed",
                )
                return metadata

            add = _run_git(repo, "add", "-A", "--", ".", env=env)
            if add.returncode != 0:
                metadata.update(
                    status="error",
                    base_commit=base,
                    error=add.stderr.strip() or "git add failed",
                )
                return metadata

            name_only = _run_git(
                repo,
                "diff",
                "--cached",
                "--name-only",
                "--find-renames",
                base,
                "--",
                ".",
                env=env,
            )
            if name_only.returncode != 0:
                metadata.update(
                    status="error",
                    base_commit=base,
                    error=name_only.stderr.strip() or "git diff --name-only failed",
                )
                return metadata

            diff = _run_git(
                repo,
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--find-renames",
                "--no-ext-diff",
                base,
                "--",
                ".",
                env=env,
            )
            if diff.returncode != 0:
                metadata.update(
                    status="error",
                    base_commit=base,
                    error=diff.stderr.strip() or "git diff failed",
                )
                return metadata

            changed_files = [
                line.strip()
                for line in name_only.stdout.splitlines()
                if line.strip() and line.strip() != _base_commit_file()
            ]
            patch_bytes = diff.stdout.encode("utf-8")
            patch_path.write_bytes(patch_bytes)
            metadata.update(
                status="empty" if not patch_bytes else "ok",
                base_commit=base,
                changed_files=changed_files,
                patch_bytes=len(patch_bytes),
                patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
            )
    except Exception as exc:
        metadata.update(status="error", error=str(exc))
    finally:
        try:
            _write_json(metadata_path, metadata)
        except Exception:
            pass
    return metadata

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PATCH_EXPORTS = {
    "mathics": _load_module(
        "refactor_patch_export_mathics",
        ROOT
        / "tasks"
        / "Mathics3__mathics-core__a34ebdf__1643"
        / "tests"
        / "patch_export.py",
    ),
    "ggshield": _load_module(
        "refactor_patch_export_ggshield",
        ROOT
        / "tasks"
        / "GitGuardian__ggshield__41a2258__947"
        / "tests"
        / "patch_export.py",
    ),
}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "eval@example.com")
    _git(repo, "config", "user.name", "Eval Bot")

    (repo / "tracked.txt").write_text("tracked base\n", encoding="utf-8")
    (repo / "delete_me.txt").write_text("delete base\n", encoding="utf-8")
    (repo / "rename_me.txt").write_text("rename base\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\x00base\xff")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "baseline")
    base_commit = _git(repo, "rev-parse", "HEAD")

    info_dir = repo / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "exclude").write_text(".base_commit\n", encoding="utf-8")
    (repo / ".base_commit").write_text(f"{base_commit}\n", encoding="utf-8")

    return base_commit


@pytest.mark.parametrize(
    "patch_export_module",
    list(PATCH_EXPORTS.values()),
    ids=list(PATCH_EXPORTS),
)
def test_export_patch_captures_committed_and_worktree_changes(
    tmp_path: Path,
    patch_export_module,
) -> None:
    repo = tmp_path / "repo"
    logs = tmp_path / "logs"
    base_commit = _init_repo(repo)

    (repo / "committed.txt").write_text("committed change\n", encoding="utf-8")
    _git(repo, "add", "committed.txt")
    _git(repo, "commit", "-m", "committed change")

    (repo / "tracked.txt").write_text("tracked changed\n", encoding="utf-8")
    (repo / "delete_me.txt").unlink()
    (repo / "rename_me.txt").rename(repo / "renamed.txt")
    (repo / "new.bin").write_bytes(b"\x00new\xff")

    metadata = patch_export_module.export_patch(repo, logs)
    patch_path = logs / patch_export_module.PATCH_FILENAME
    metadata_path = logs / patch_export_module.PATCH_METADATA_FILENAME
    patch_text = patch_path.read_text(encoding="utf-8")

    assert metadata["status"] == "ok"
    assert metadata["base_commit"] == base_commit
    assert metadata_path.exists()
    assert patch_path.exists()
    assert "committed.txt" in patch_text
    assert "tracked.txt" in patch_text
    assert "deleted file mode" in patch_text
    assert "rename from rename_me.txt" in patch_text
    assert "rename to renamed.txt" in patch_text
    assert "GIT binary patch" in patch_text
    assert metadata["patch_bytes"] == len(patch_text.encode("utf-8"))
    assert metadata["patch_sha256"] == hashlib.sha256(
        patch_text.encode("utf-8")
    ).hexdigest()
    assert "committed.txt" in metadata["changed_files"]
    assert "tracked.txt" in metadata["changed_files"]
    assert "new.bin" in metadata["changed_files"]

    (repo / ".cache_ggshield").mkdir()
    (repo / ".cache_ggshield" / "noise.txt").write_text("cache\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "python").write_text("virtualenv\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "noise.pyc").write_bytes(b"pyc")

    repo_metadata = patch_export_module.export_repo(repo, logs)
    repo_path = logs / patch_export_module.REPO_EXPORT_DIRNAME
    repo_metadata_path = logs / patch_export_module.REPO_METADATA_FILENAME

    assert repo_metadata["status"] == "ok"
    assert repo_metadata_path.exists()
    assert repo_path.exists()
    assert repo_metadata["file_count"] > 0
    assert repo_metadata["repo_bytes"] > 0
    assert (
        (repo_path / "tracked.txt").read_text(encoding="utf-8")
        == "tracked changed\n"
    )
    assert (
        (repo_path / "committed.txt").read_text(encoding="utf-8")
        == "committed change\n"
    )
    assert (repo_path / "renamed.txt").read_text(encoding="utf-8") == "rename base\n"
    assert not (repo_path / "delete_me.txt").exists()
    assert (repo_path / "new.bin").read_bytes() == b"\x00new\xff"
    assert not (repo_path / ".git").exists()
    assert not (repo_path / ".base_commit").exists()
    assert not (repo_path / ".venv").exists()
    assert not (repo_path / ".cache_ggshield").exists()
    assert not (repo_path / "__pycache__").exists()


@pytest.mark.parametrize(
    "patch_export_module",
    list(PATCH_EXPORTS.values()),
    ids=list(PATCH_EXPORTS),
)
def test_export_patch_writes_empty_patch_for_clean_repo(
    tmp_path: Path,
    patch_export_module,
) -> None:
    repo = tmp_path / "repo"
    logs = tmp_path / "logs"
    _init_repo(repo)

    metadata = patch_export_module.export_patch(repo, logs)
    patch_path = logs / patch_export_module.PATCH_FILENAME

    assert metadata["status"] == "empty"
    assert metadata["changed_files"] == []
    assert patch_path.exists()
    assert patch_path.read_text(encoding="utf-8") == ""
    assert metadata["patch_bytes"] == 0

    repo_metadata = patch_export_module.export_repo(repo, logs)
    repo_path = logs / patch_export_module.REPO_EXPORT_DIRNAME

    assert repo_metadata["status"] == "ok"
    assert (
        (repo_path / "tracked.txt").read_text(encoding="utf-8") == "tracked base\n"
    )
    assert not (repo_path / ".git").exists()
    assert not (repo_path / ".base_commit").exists()

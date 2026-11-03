from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_TESTS = (
    ROOT
    / "tasks"
    / "scrapy-scrapy-pr-6546-core-bd634698-v0102-prepared-v002-triple"
    / "tests"
)


def _load_leakage():
    spec = importlib.util.spec_from_file_location(
        "scrapy_leakage_under_test",
        TASK_TESTS / "hard_gates" / "leakage.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Scrapy leakage audit")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture(
    tmp_path: Path,
    *,
    tool_output: str,
) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repo"
    public_file = repository / "scrapy/core/downloader/contextfactory.py"
    public_file.parent.mkdir(parents=True)
    public_file.write_text(
        "class PublicContextFactory:\n"
        "    # public tls context factory behavior\n"
        "    pass\n",
        encoding="utf-8",
    )

    evaluation_assets = tmp_path / "evaluation_assets"
    fault = evaluation_assets / "faults/patches/F001-hidden.patch"
    fault.parent.mkdir(parents=True)
    fault.write_text(
        """\
diff --git a/scrapy/core/downloader/contextfactory.py b/scrapy/core/downloader/contextfactory.py
index 1111111..2222222 100644
--- a/scrapy/core/downloader/contextfactory.py
+++ b/scrapy/core/downloader/contextfactory.py
@@ -1,2 +1,3 @@
+    # public tls context factory behavior
+    return "hidden tls version bypass marker"
+class PublicContextFactory:
""",
        encoding="utf-8",
    )

    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text(
        json.dumps(
            {
                "steps": [
                    {"source": "user", "message": "Inspect the public repository."},
                    {
                        "source": "agent",
                        "message": "",
                        "observation": {
                            "results": [{"content": tool_output}],
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return trajectory, evaluation_assets, repository


def test_public_git_diff_headers_are_not_answer_leakage(tmp_path: Path) -> None:
    leakage = _load_leakage()
    path = "scrapy/core/downloader/contextfactory.py"
    trajectory, evaluation_assets, repository = _write_fixture(
        tmp_path,
        tool_output=(
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "+    # public tls context factory behavior\n"
        ),
    )

    report, validation = leakage.audit_inbound_content(
        trajectory_path=trajectory,
        evaluation_assets=evaluation_assets,
        public_roots=[repository],
        candidate_id="candidate-public-diff",
    )

    assert report["status"] == "pass"
    assert report["answer_leakage_status"] == "clean"
    assert report["finding_count"] == 0
    assert validation["status"] == "pass"


def test_hidden_patch_content_remains_answer_leakage(tmp_path: Path) -> None:
    leakage = _load_leakage()
    path = "scrapy/core/downloader/contextfactory.py"
    trajectory, evaluation_assets, repository = _write_fixture(
        tmp_path,
        tool_output=(
            f"diff --git a/{path} b/{path}\n"
            "+    # public tls context factory behavior\n"
            '+    return "hidden tls version bypass marker"\n'
        ),
    )

    report, validation = leakage.audit_inbound_content(
        trajectory_path=trajectory,
        evaluation_assets=evaluation_assets,
        public_roots=[repository],
        candidate_id="candidate-hidden-content",
    )

    assert report["status"] == "invalid"
    assert report["answer_leakage_status"] == "confirmed_pipeline_exposure"
    assert report["finding_count"] == 1
    assert report["findings"][0]["exposed_information_summary"] == (
        '+ return "hidden tls version bypass marker"'
    )
    assert validation["status"] == "fail"

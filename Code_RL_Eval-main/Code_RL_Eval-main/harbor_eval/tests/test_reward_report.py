from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPORT = _load_module("harbor_eval_reward_report", ROOT / "reward_report.py")
RUN = _load_module("harbor_eval_run_for_reward_report", ROOT / "run.py")


def _reward_details() -> dict:
    return {
        "task_id": "Example__repo__case",
        "status": "pass",
        "trainable": True,
        "invalid": False,
        "reward": 0.7795588235294117,
        "objective_score": 0.9707308377896612,
        "objective_rewardkit_score": 0.9707,
        "reason": "LLM rubric score calculated independently",
        "hard_gates": {
            "failed_gates": [],
            "uncertain_gates": [],
            "gates": {
                "regression_pass": {
                    "applicable": True,
                    "status": "pass",
                    "reason": "focused regression command passed",
                }
            },
        },
        "objective_metrics": {
            "status": "ok",
            "metrics": {
                "focused_behavior_evidence": {
                    "baseline": 1.0,
                    "candidate": 0.6667,
                    "delta": -0.3333,
                    "applicable": True,
                    "status": "ok",
                    "evidence": {"source": "oracle comparison"},
                }
            },
        },
        "objective_rewardkit": {
            "criteria": [
                {
                    "id": "C1.1",
                    "name": "behavior",
                    "raw": 0.6667,
                    "value": 0.6667,
                    "weight": 2.0,
                    "hard": False,
                    "reasoning": "objective proxy has partial coverage",
                }
            ]
        },
        "rubric": {
            "score": 0.7795588235294117,
            "raw_score": 0.7795588235294117,
            "rewardkit_score": 0.7795588235294117,
            "criteria": [
                {
                    "id": "C1.1",
                    "name": "behavior",
                    "raw": 1.0,
                    "value": 1.0,
                    "weight": 2.0,
                    "hard": True,
                    "reasoning": "behavior preserved",
                },
                {
                    "id": "C1.2",
                    "name": "edge_cases",
                    "raw": 0.5,
                    "value": 0.5,
                    "weight": 1.0,
                    "hard": False,
                    "reasoning": "edge-case evidence is incomplete",
                },
            ],
        },
        "patch_export": {
            "status": "ok",
            "base_commit": "abc123",
            "patch_bytes": 42,
            "patch_path": "/logs/verifier/refactor.patch",
            "changed_files": ["src/a.py", "tests/test_a.py"],
        },
        "repo_export": {
            "status": "ok",
            "file_count": 12,
            "repo_bytes": 3456,
        },
    }


def _write_job_result(
    job_dir: Path,
    eval_name: str = "codex__gpt-5.5__adhoc",
    reward_stats: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    eval_record: dict[str, object] = {
        "n_trials": 2,
        "n_errors": 1,
        "metrics": [
            {"objective": 0.8, "reward": 0.7, "rubric": 0.6}
        ],
        "exception_stats": {"RuntimeError": ["trial-b"]},
    }
    if reward_stats is not None:
        eval_record["reward_stats"] = reward_stats
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "id": f"result-{job_dir.name}",
                "started_at": "2026-07-31T09:00:00",
                "finished_at": "2026-07-31T09:10:00",
                "n_total_trials": 2,
                "stats": {
                    "n_completed_trials": 2,
                    "n_errored_trials": 1,
                    "n_running_trials": 0,
                    "n_pending_trials": 0,
                    "n_cancelled_trials": 0,
                    "n_retries": 0,
                    "n_input_tokens": 1000,
                    "n_cache_tokens": 900,
                    "n_output_tokens": 100,
                    "cost_usd": 1.25,
                    "evals": {
                        eval_name: eval_record,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_render_reward_details_markdown_contains_human_summary() -> None:
    text = REPORT.render_reward_details_markdown(_reward_details())

    assert "# Reward Score Details" in text
    assert "| Reward | 0.7796 |" in text
    assert "regression_pass" in text
    assert "focused regression command passed" in text
    assert "C1.2" in text
    assert "edge-case evidence is incomplete" in text
    assert "focused_behavior_evidence" in text
    assert "src/a.py" in text


def test_run_helper_writes_markdown_reports_for_job_dir(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job-a"
    _write_job_result(job_dir)
    verifier_dir = job_dir / "trial-a" / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward-details.json").write_text(
        json.dumps(_reward_details(), ensure_ascii=False),
        encoding="utf-8",
    )

    reports = RUN._try_write_reward_reports(job_dir)

    assert reports == [
        verifier_dir / REPORT.REPORT_FILENAME,
        job_dir / REPORT.JOB_REPORT_FILENAME,
    ]
    assert (verifier_dir / REPORT.REPORT_FILENAME).is_file()
    assert (job_dir / REPORT.JOB_REPORT_FILENAME).is_file()


def test_render_reports_for_job_dir_writes_job_summary(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job-a"
    _write_job_result(job_dir)
    verifier_dir = job_dir / "trial-a" / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward-details.json").write_text(
        json.dumps(_reward_details(), ensure_ascii=False),
        encoding="utf-8",
    )
    (job_dir / "trial-b").mkdir()
    (job_dir / "trial-b" / "result.json").write_text(
        json.dumps({"exception_info": {"message": "verifier failed"}}),
        encoding="utf-8",
    )

    reports = REPORT.render_reports_for_path(job_dir)
    job_report = job_dir / REPORT.JOB_REPORT_FILENAME

    assert job_report in reports
    text = job_report.read_text(encoding="utf-8")
    assert "# Harbor Job Score Report" in text
    assert "codex__gpt-5.5__adhoc" in text
    assert "## Model Summary" in text
    assert "gpt-5.5" in text
    assert "trial-a" in text
    assert "trial-b" in text
    assert "RuntimeError" in text
    assert "verifier/reward-details.md" in text


def test_render_reports_for_batch_dir_writes_batch_summary(tmp_path: Path) -> None:
    batch_dir = tmp_path / "jobs" / "batch-a"
    job_a = batch_dir / "job-a"
    job_b = batch_dir / "job-b"
    _write_job_result(job_a, "codex__gpt-5.4__adhoc")
    _write_job_result(job_b, "codex__gpt-5.5__adhoc")

    reports = REPORT.render_reports_for_path(batch_dir)
    batch_report = batch_dir / REPORT.BATCH_REPORT_FILENAME

    assert job_a / REPORT.JOB_REPORT_FILENAME in reports
    assert job_b / REPORT.JOB_REPORT_FILENAME in reports
    assert batch_report in reports
    text = batch_report.read_text(encoding="utf-8")
    assert "# Harbor Batch Score Report" in text
    assert "## Overview" in text
    assert "## Model Summary" in text
    assert "## Low-Score Trials" in text
    assert "job-a" in text
    assert "job-b" in text
    assert "codex__gpt-5.4__adhoc" in text
    assert "codex__gpt-5.5__adhoc" in text


def test_render_batch_report_marks_candidate_failures_as_failed(tmp_path: Path) -> None:
    batch_dir = tmp_path / "jobs" / "batch-a"
    job_a = batch_dir / "job-a"
    job_b = batch_dir / "job-b"
    _write_job_result(
        job_a,
        "codex__gpt-5.4__adhoc",
        reward_stats={"candidate_fail": {"1.0": ["trial-a"]}},
    )
    _write_job_result(job_b, "codex__gpt-5.5__adhoc")

    reports = REPORT.render_reports_for_path(batch_dir)
    batch_report = batch_dir / REPORT.BATCH_REPORT_FILENAME

    assert batch_report in reports
    text = batch_report.read_text(encoding="utf-8")
    assert "Status | Failed (`fail`)" in text


def test_render_reports_for_single_job_batch_writes_batch_summary(tmp_path: Path) -> None:
    batch_dir = tmp_path / "jobs" / "batch-a"
    job_dir = batch_dir / "job-a"
    _write_job_result(job_dir)

    reports = REPORT.render_reports_for_path(batch_dir)

    assert job_dir / REPORT.JOB_REPORT_FILENAME in reports
    assert batch_dir / REPORT.BATCH_REPORT_FILENAME in reports


def test_render_job_report_marks_candidate_failures_as_failed(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job-a"
    _write_job_result(
        job_dir,
        reward_stats={"candidate_fail": {"1.0": ["trial-a"]}},
    )

    reports = REPORT.render_reports_for_path(job_dir)
    job_report = job_dir / REPORT.JOB_REPORT_FILENAME

    assert job_report in reports
    text = job_report.read_text(encoding="utf-8")
    assert "Status | Failed (`fail`)" in text


def test_render_reports_for_trial_dir_does_not_write_job_report(tmp_path: Path) -> None:
    trial_dir = tmp_path / "jobs" / "job-a" / "trial-a"
    verifier_dir = trial_dir / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward-details.json").write_text(
        json.dumps(_reward_details(), ensure_ascii=False),
        encoding="utf-8",
    )
    (trial_dir / "result.json").write_text(
        json.dumps({"trial_name": "trial-a", "exception_info": {}}),
        encoding="utf-8",
    )

    reports = REPORT.render_reports_for_path(trial_dir)

    assert reports == [verifier_dir / REPORT.REPORT_FILENAME]
    assert not (trial_dir / REPORT.JOB_REPORT_FILENAME).exists()

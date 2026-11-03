from __future__ import annotations

import importlib.util
import json
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


RUN = _load_module("harbor_eval_run", ROOT / "run.py")


def test_make_job_name_omits_timestamp_and_keeps_agent_model_random_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedUUID:
        hex = "abcdef1234567890"

    monkeypatch.setattr(RUN, "uuid4", lambda: FixedUUID())

    assert RUN._make_job_name("codex", "gpt-5.5") == "codex__gpt-5.5__abcdef"


def test_make_batch_name_keeps_timestamp_task_batch_and_random_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls):
            return cls()

        def strftime(self, fmt: str) -> str:
            assert fmt == "%Y-%m-%d__%H-%M-%S"
            return "2026-07-31__12-34-56"

    class FixedUUID:
        hex = "123456abcdef7890"

    task_path = tmp_path / "GitGuardian__ggshield__41a2258__947"

    monkeypatch.setattr(RUN, "datetime", FixedDateTime)
    monkeypatch.setattr(RUN, "uuid4", lambda: FixedUUID())

    assert (
        RUN._make_batch_name(task_path)
        == "2026-07-31__12-34-56__GitGuardian__ggshield__41a2258__947__batch__123456"
    )


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_job_root(
    jobs_root: Path,
    job_name: str,
    *,
    total_trials: int,
    completed_trials: int,
    errored_trials: int,
    metrics: dict[str, dict[float, list[str]]],
    started_at: str,
    finished_at: str,
    cost_usd: float | None = None,
) -> Path:
    job_dir = jobs_root / job_name
    _write_json(
        job_dir / "result.json",
        {
            "id": "job-1",
            "started_at": started_at,
            "finished_at": finished_at,
            "n_total_trials": total_trials,
            "stats": {
                "n_completed_trials": completed_trials,
                "n_errored_trials": errored_trials,
                "n_running_trials": 0,
                "n_pending_trials": 0,
                "n_cancelled_trials": 0,
                "n_retries": 0,
                "evals": {
                    "codex__gpt-5.5__adhoc": {
                        "n_trials": completed_trials,
                        "n_errors": errored_trials,
                        "metrics": [
                            {
                                "objective": 0.0,
                                "reward": 0.0,
                                "rubric": 0.0,
                            }
                        ],
                        "reward_stats": {
                            metric: {
                                str(score): names for score, names in values.items()
                            }
                            for metric, values in metrics.items()
                        },
                        "exception_stats": {},
                    }
                },
                "n_input_tokens": 1,
                "n_cache_tokens": 2,
                "n_output_tokens": 3,
                "cost_usd": cost_usd,
            },
        },
    )
    return job_dir


def _build_trial(
    job_dir: Path,
    trial_name: str,
    *,
    started_at: str,
    finished_at: str,
    reward: float,
    objective: float,
    rubric: float,
    reward_details: dict,
    exception_info: dict | None = None,
) -> None:
    trial_dir = job_dir / trial_name
    _write_json(
        trial_dir / "result.json",
        {
            "id": f"{trial_name}-id",
            "task_name": "task",
            "trial_name": trial_name,
            "trial_uri": f"file://{trial_dir}",
            "task_id": {"path": "/tmp/task"},
            "source": None,
            "task_checksum": "checksum",
            "config": {"task": {"path": "/tmp/task"}, "trial_name": trial_name},
            "agent_info": {
                "name": "codex",
                "version": "0.1",
                "model_info": {"name": "gpt-5.5", "provider": None},
            },
            "agent_result": {"n_input_tokens": 1, "n_cache_tokens": 2, "n_output_tokens": 3, "cost_usd": 4.0},
            "verifier_result": {
                "rewards": {
                    "objective": objective,
                    "reward": reward,
                    "rubric": rubric,
                }
            },
            "exception_info": exception_info,
            "started_at": started_at,
            "finished_at": finished_at,
            "environment_setup": None,
            "agent_setup": None,
            "agent_execution": None,
            "verifier": None,
            "step_results": None,
        },
    )
    _write_json(trial_dir / "verifier" / "reward.json", {"objective": objective, "reward": reward, "rubric": rubric})
    _write_json(trial_dir / "verifier" / "reward-details.json", reward_details)


def _build_candidate_failure_trial(job_dir: Path, trial_name: str) -> None:
    trial_dir = job_dir / trial_name
    _write_json(
        trial_dir / "result.json",
        {
            "trial_name": trial_name,
            "exception_info": None,
            "verifier_result": {"rewards": {"candidate_fail": 1.0}},
            "started_at": "2026-08-06T20:12:13Z",
            "finished_at": "2026-08-06T20:18:12Z",
            "environment_setup": None,
            "agent_setup": None,
            "agent_execution": {
                "started_at": "2026-08-06T20:12:13Z",
                "finished_at": "2026-08-06T20:18:12Z",
            },
            "verifier": {
                "started_at": "2026-08-06T20:18:16Z",
                "finished_at": "2026-08-06T20:18:22Z",
            },
        },
    )
    _write_json(trial_dir / "verifier" / "reward.json", {"candidate_fail": 1.0})
    _write_json(
        trial_dir / "verifier" / "verdict.json",
        {
            "status": "candidate_fail",
            "classification": "candidate_boundary",
            "official_reward": False,
        },
    )
    _write_json(
        trial_dir / "verifier" / "candidate-boundary-validation.json",
        {
            "status": "fail",
            "mode": "strict",
            "violations": ["unsupported_tool:request_user_input"],
        },
    )


def _base_trial_result() -> dict:
    timestamp = "2026-07-29T00:00:01Z"
    return {
        "environment_setup": {"started_at": timestamp, "finished_at": timestamp},
        "agent_setup": {"started_at": timestamp, "finished_at": timestamp},
        "agent_execution": {"started_at": timestamp, "finished_at": timestamp},
        "verifier": {"started_at": timestamp},
    }


def test_trial_phase_tracks_pipeline_stages(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial-a"
    verifier_dir = trial_dir / "verifier"
    verifier_dir.mkdir(parents=True)

    assert (
        RUN._trial_phase(
            {"environment_setup": {"started_at": "2026-07-29T00:00:01Z"}},
            trial_dir,
            {},
        )
        == "Docker build in progress"
    )
    assert (
        RUN._trial_phase(
            {
                "environment_setup": {
                    "started_at": "2026-07-29T00:00:01Z",
                    "finished_at": "2026-07-29T00:00:02Z",
                },
                "agent_setup": {
                    "started_at": "2026-07-29T00:00:02Z",
                    "finished_at": "2026-07-29T00:00:03Z",
                },
                "agent_execution": {"started_at": "2026-07-29T00:00:03Z"},
            },
            trial_dir,
            {},
        )
        == "Agent running"
    )

    result = _base_trial_result()
    assert RUN._trial_phase(result, trial_dir, {}) == "Hard-gate running"

    _write_json(verifier_dir / "sc-e1.json", {})
    assert RUN._trial_phase(result, trial_dir, {}) == "Objective running"

    _write_json(verifier_dir / "sc-j1.json", {})
    assert RUN._trial_phase(result, trial_dir, {}) == "Rubric running"

    _write_json(verifier_dir / "reward.json", {"reward": 1.0})
    assert RUN._trial_phase(result, trial_dir, {}) == "Reward running"


def test_trial_phase_identifies_docker_build_failures(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial-a"

    phase = RUN._trial_phase(
        {
            "environment_setup": {
                "started_at": "2026-07-29T00:00:01Z",
                "finished_at": "2026-07-29T00:00:02Z",
            }
        },
        trial_dir,
        {
            "exception_message": "Docker compose command failed for environment x",
            "exception_traceback": "Traceback ... _setup_agent_environment ...",
        },
    )

    assert phase == "Docker build failed"


def test_job_phase_uses_partial_verifier_artifacts_for_running_trials(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "job-a"
    trial_dir = job_dir / "trial-a"
    _write_json(trial_dir / "verifier" / "sc-e1.json", {})
    summary = RUN.JobSummary(
        job_name="job-a",
        job_dir=job_dir,
        total_trials=1,
        completed_trials=0,
        errored_trials=0,
        running_trials=1,
        pending_trials=0,
        cancelled_trials=0,
        retries=0,
        duration="Unknown",
        cost_usd=None,
        n_input_tokens=None,
        n_cache_tokens=None,
        n_output_tokens=None,
        trials=[],
    )
    prepared = RUN.PreparedRun(
        index=0,
        agent="codex",
        model="gpt-5.6-sol",
        task_path=tmp_path / "task",
        job_name="job-a",
        job_dir=job_dir,
        log_path=job_dir / "run.log",
        cmd=["harbor", "run"],
        process_env={},
        concurrent=1,
        attempts=1,
        judge_model="gpt-5.5",
        judge_reasoning_effort="high",
        setup_timeout_multiplier=None,
    )
    prepared.process = object()

    assert RUN._job_phase(prepared, summary) == "Objective running"


def test_partial_trial_phase_prefers_agent_artifacts_over_empty_verifier_dir(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "trial-a"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "verifier").mkdir(parents=True)
    _write_json(trial_dir / "agent" / "codex.txt", {"status": "running"})

    assert RUN._partial_trial_phase(trial_dir) == "Agent running"


def test_load_job_summary_handles_zero_reward_and_exception_diagnosis(tmp_path: Path) -> None:
    job_dir = _build_job_root(
        tmp_path / "jobs",
        "2026-07-29__00-00-00__codex__gpt-5.5__abc123",
        total_trials=2,
        completed_trials=2,
        errored_trials=0,
        metrics={
            "objective": {0.0: ["trial-a", "trial-b"]},
            "rubric": {0.0: ["trial-a", "trial-b"]},
            "reward": {0.0: ["trial-a", "trial-b"]},
        },
        started_at="2026-07-29T00:00:00",
        finished_at="2026-07-29T00:10:00",
        cost_usd=1.23,
    )

    _build_trial(
        job_dir,
        "trial-a",
        started_at="2026-07-29T00:00:01Z",
        finished_at="2026-07-29T00:01:01Z",
        reward=0.0,
        objective=0.0,
        rubric=0.0,
        reward_details={
            "status": "uncertain",
            "reason": "hard-gate evidence uncertain; sample is not trainable",
            "trainable": False,
            "hard_gates": {
                "failed_gates": [],
                "uncertain_gates": ["regression_pass"],
                "gates": {
                    "regression_pass": {"reason": "diff command failed rc=-2"}
                },
            },
        },
    )
    _build_trial(
        job_dir,
        "trial-b",
        started_at="2026-07-29T00:02:01Z",
        finished_at="2026-07-29T00:03:01Z",
        reward=0.0,
        objective=0.0,
        rubric=0.0,
        reward_details={
            "status": "failed_hard_gate",
            "reason": "hard gate failed",
            "trainable": False,
            "hard_gates": {
                "failed_gates": ["structure_ast_pass"],
                "uncertain_gates": ["no_new_skip_or_xfail"],
                "gates": {
                    "structure_ast_pass": {"reason": "1 target file(s) missing"},
                    "no_new_skip_or_xfail": {"reason": "diff command failed rc=-2"},
                },
            },
        },
    )

    summary = RUN._load_job_summary(job_dir)
    assert summary is not None
    assert summary.metrics["reward"].average == 0.0
    assert summary.metrics["reward"].distribution == [(0.0, 2)]
    assert summary.trials[0].status == "Failed"
    assert summary.trials[1].status == "Failed"
    assert "all samples are untrainable" in RUN._summary_conclusion(summary)


def test_load_job_summary_marks_candidate_failures_as_failed(
    tmp_path: Path,
) -> None:
    job_dir = _build_job_root(
        tmp_path / "jobs",
        "2026-08-06__20-12-10__codex__gpt-5.6-sol__candidate",
        total_trials=1,
        completed_trials=1,
        errored_trials=0,
        metrics={"candidate_fail": {1.0: ["trial-a"]}},
        started_at="2026-08-06T20:12:10",
        finished_at="2026-08-06T20:18:22",
    )
    _build_candidate_failure_trial(job_dir, "trial-a")

    summary = RUN._load_job_summary(job_dir)
    assert summary is not None
    assert summary.candidate_fail_trials == 1
    assert summary.trials[0].status == "Failed"
    assert summary.trials[0].candidate_failed is True
    assert "candidate_fail (candidate_boundary)" in summary.trials[0].reason
    assert "candidate failures" in RUN._summary_conclusion(summary)

    prepared = RUN.PreparedRun(
        index=0,
        agent="codex",
        model="gpt-5.6-sol",
        task_path=tmp_path / "task",
        job_name=job_dir.name,
        job_dir=job_dir,
        log_path=job_dir / "run.log",
        cmd=["harbor", "run"],
        process_env={},
        concurrent=1,
        attempts=1,
        judge_model="gpt-5.5",
        judge_reasoning_effort="high",
        setup_timeout_multiplier=None,
        job_summary=summary,
    )
    prepared.process = object()
    prepared.exit_code = 0

    outcome = RUN.RunOutcome(exit_code=0, job_summary=summary)
    assert RUN._prepared_run_state(prepared) == ("Failed(candidate failure)", "red")
    assert RUN._job_phase(prepared, summary) == "Candidate failure"
    assert RUN._outcome_status(outcome) == "❌ Failed(candidate failure)"
    assert RUN._outcome_failed(outcome)
    assert RUN._outcome_exit_code(outcome) == 1


def test_print_readable_summary_omits_cost(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    job_dir = _build_job_root(
        tmp_path / "jobs",
        "2026-07-29__00-00-00__codex__gpt-5.5__ghi789",
        total_trials=1,
        completed_trials=1,
        errored_trials=0,
        metrics={
            "objective": {1.0: ["trial-a"]},
            "rubric": {1.0: ["trial-a"]},
            "reward": {1.0: ["trial-a"]},
        },
        started_at="2026-07-29T00:00:00",
        finished_at="2026-07-29T00:01:00",
        cost_usd=9.99,
    )

    _build_trial(
        job_dir,
        "trial-a",
        started_at="2026-07-29T00:00:01Z",
        finished_at="2026-07-29T00:01:01Z",
        reward=1.0,
        objective=1.0,
        rubric=1.0,
        reward_details={
            "status": "passed",
            "reason": "all checks passed",
            "trainable": True,
            "patch_export": {
                "status": "ok",
                "changed_files": ["mathics/core/foo.py", "mathics/core/bar.py"],
            },
        },
    )

    summary = RUN._load_job_summary(job_dir)
    assert summary is not None
    assert summary.trials[0].changed_files_count == 2

    RUN._print_readable_summary(summary)
    captured = capsys.readouterr().out
    assert "Cost" not in captured
    assert "Files=2" in captured
    assert "Tokens: input 1, cache 2, output 3" in captured


def test_load_job_summary_handles_partial_scores_and_reasoning(tmp_path: Path) -> None:
    job_dir = _build_job_root(
        tmp_path / "jobs",
        "2026-07-29__00-00-00__codex__gpt-5.5__def456",
        total_trials=3,
        completed_trials=3,
        errored_trials=0,
        metrics={
            "objective": {0.36: ["trial-a", "trial-b", "trial-c"]},
            "rubric": {0.25: ["trial-a", "trial-b"], 0.5: ["trial-c"]},
            "reward": {0.25: ["trial-a", "trial-b"], 0.5: ["trial-c"]},
        },
        started_at="2026-07-29T00:00:00",
        finished_at="2026-07-29T00:30:00",
        cost_usd=4.56,
    )

    for trial_name, reward, finished_at in (
        ("trial-a", 0.25, "2026-07-29T00:05:00Z"),
        ("trial-b", 0.25, "2026-07-29T00:10:00Z"),
        ("trial-c", 0.5, "2026-07-29T00:15:00Z"),
    ):
        _build_trial(
            job_dir,
            trial_name,
            started_at="2026-07-29T00:00:01Z",
            finished_at=finished_at,
            reward=reward,
            objective=0.36,
            rubric=reward,
            reward_details={
                "objective": {
                    "score": 0.36,
                    "criteria": [
                        {
                            "name": "type_check",
                            "value": 1.0,
                            "raw": 1.0,
                            "weight": 1.0,
                            "description": "Mypy accepts all changed production Python files",
                        },
                        {
                            "name": "focused_tests",
                            "value": 0.0,
                            "raw": 0.0,
                            "weight": 2.0,
                            "reasoning": "Focused Graphics and formatting regression tests still fail.",
                        },
                    ],
                },
                "rubric": {
                    "score": reward,
                    "criteria": [
                        {
                            "id": "behavior",
                            "name": "behavior",
                            "value": reward,
                            "raw": reward,
                            "weight": 3.0,
                            "reasoning": "Behavior evidence is partial but credible.",
                        }
                    ],
                },
            },
        )

    summary = RUN._load_job_summary(job_dir)
    assert summary is not None
    assert summary.metrics["objective"].average == pytest.approx(0.36)
    assert summary.metrics["reward"].average == pytest.approx(1 / 3)
    assert summary.metrics["reward"].distribution == [(0.25, 2), (0.5, 1)]
    assert summary.trials[0].status == "Passed"
    assert "focused_tests" in summary.trials[0].reason
    assert "still fail" in summary.trials[0].reason
    assert "Reward > 0 trials 3/3" in RUN._summary_conclusion(summary)


def test_build_harbor_command_includes_jobs_dir_and_envs(tmp_path: Path) -> None:
    cmd = RUN.build_harbor_command(
        harbor_cli="/opt/homebrew/bin/harbor",
        task_path=tmp_path / "task",
        model="claude-opus-4-8",
        agent="claude-code",
        concurrent=5,
        attempts=2,
        jobs_dir=tmp_path / "jobs",
        job_name="2026-07-30__12-00-00__claude-code__claude-opus-4-8__abc123",
        openai_api_key="openai-key",
        openai_base_url="https://example.invalid/v1",
        anthropic_base_url="https://anthropic.invalid",
        anthropic_auth_token="anthropic-token",
        setup_timeout_multiplier=3.0,
        judge_model="gpt-5.5",
        judge_reasoning_effort="high",
        verifier_run_plan_toml='[verifier]\nrubric_weight = 1.0\n',
    )

    assert cmd[:2] == ["/opt/homebrew/bin/harbor", "run"]
    assert cmd[cmd.index("--jobs-dir") + 1] == str(tmp_path / "jobs")
    assert cmd[cmd.index("--job-name") + 1].endswith("__abc123")
    assert "OPENAI_API_KEY=openai-key" in cmd
    assert "OPENAI_BASE_URL=https://example.invalid/v1" in cmd
    assert "ANTHROPIC_BASE_URL=https://anthropic.invalid" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=anthropic-token" in cmd
    assert "HARBOR_RUN_PLAN_TOML=[verifier]\nrubric_weight = 1.0\n" in cmd
    assert cmd[cmd.index("--agent-setup-timeout-multiplier") + 1] == "3.0"


def test_build_harbor_command_includes_live_judge_envs(tmp_path: Path) -> None:
    cmd = RUN.build_harbor_command(
        harbor_cli="/opt/homebrew/bin/harbor",
        task_path=tmp_path / "task",
        model="gpt-5.6-sol",
        agent="codex",
        concurrent=1,
        attempts=1,
        jobs_dir=tmp_path / "jobs",
        job_name="job",
        openai_api_key="openai-key",
        openai_base_url="https://example.invalid/v1",
        judge_mode="live",
        unit_test_rl_mindflow_api_key="mindflow-key",
    )

    assert "UNITTEST_RL_JUDGE_MODE=live" in cmd
    assert "UNITTEST_RL_MINDFLOW_API_KEY=mindflow-key" in cmd
    assert "UNITTEST_RL_JUDGE_API_KEY=mindflow-key" in cmd


def test_dry_run_skips_cli_and_env_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(RUN, "find_harbor_cli", lambda: None)

    def fail_run(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("subprocess.run must not be called in dry-run mode")

    monkeypatch.setattr(RUN.subprocess, "run", fail_run)

    outcome = RUN.run_harbor_task(
        task_path=task_path,
        dry_run=True,
        jobs_dir=tmp_path / "jobs",
    )

    assert outcome.exit_code == 0
    captured = capsys.readouterr().out
    assert "📝 Command:" in captured


def test_print_batch_summary_marks_trial_errors_as_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = RUN.JobSummary(
        job_name="job-with-errors",
        job_dir=tmp_path / "jobs" / "job-with-errors",
        total_trials=5,
        completed_trials=5,
        errored_trials=5,
        running_trials=0,
        pending_trials=0,
        cancelled_trials=0,
        retries=0,
        duration="7s",
        cost_usd=None,
        n_input_tokens=None,
        n_cache_tokens=None,
        n_output_tokens=None,
    )

    outcome = RUN.RunOutcome(exit_code=0, job_summary=summary)

    RUN._print_batch_summary([("codex", "gpt-5.5", outcome)], dry_run=False)

    captured = capsys.readouterr().out
    assert "❌ Failed(runtime exceptions)  codex + gpt-5.5" in captured
    assert "exceptions 5/5" in captured
    assert RUN._outcome_failed(outcome)


def test_render_progress_board_includes_phase_column(tmp_path: Path) -> None:
    if not RUN.RICH_AVAILABLE:
        pytest.skip("rich is not installed")

    summary = RUN.JobSummary(
        job_name="job-a",
        job_dir=tmp_path / "jobs" / "job-a",
        total_trials=1,
        completed_trials=0,
        errored_trials=0,
        running_trials=1,
        pending_trials=0,
        cancelled_trials=0,
        retries=0,
        duration="Unknown",
        cost_usd=None,
        n_input_tokens=None,
        n_cache_tokens=None,
        n_output_tokens=None,
        trials=[
            RUN.TrialSummary(
                trial_name="trial-a",
                short_name="trial-a",
                status="Unscored",
                phase="Agent running",
                objective=None,
                rubric=None,
                reward=None,
                duration="Unknown",
            )
        ],
    )
    prepared = RUN.PreparedRun(
        index=0,
        agent="codex",
        model="gpt-5.6-sol",
        task_path=tmp_path / "task",
        job_name="job-a",
        job_dir=tmp_path / "jobs" / "job-a",
        log_path=tmp_path / "jobs" / "job-a" / "run.log",
        cmd=["harbor", "run"],
        process_env={},
        concurrent=1,
        attempts=1,
        judge_model="gpt-5.5",
        judge_reasoning_effort="high",
        setup_timeout_multiplier=None,
        job_summary=summary,
    )
    prepared.process = object()

    table = RUN._render_progress_board([prepared])

    assert [column.header for column in table.columns][:3] == ["Combination", "Status", "Phase"]


def test_load_run_config_expands_agent_model_groups(tmp_path: Path) -> None:
    config_path = tmp_path / "run-plan.toml"
    config_path.write_text(
        """
schema_version = "harbor-run-plan/v1"

[[runs]]
agent = "agent1"
models = ["m1", "m2"]

[[runs]]
agent = "agent2"
models = ["m3"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    targets = RUN.load_run_config(config_path)

    assert targets == [("agent1", "m1"), ("agent1", "m2"), ("agent2", "m3")]


def test_main_uses_default_config_without_cli_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()
    config_path = tmp_path / "run-plan.toml"
    config_path.write_text(
        """
schema_version = "harbor-run-plan/v1"

[[runs]]
agent = "codex"
models = ["gpt-5.5"]

[[runs]]
agent = "claude-code"
models = ["claude-opus-4-8"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict[str, object]] = []

    def fake_run_harbor_task(**kwargs):
        calls.append(kwargs)
        return RUN.RunOutcome(exit_code=0)

    monkeypatch.setattr(RUN, "DEFAULT_RUN_CONFIG", config_path)
    monkeypatch.setattr(RUN, "run_harbor_task", fake_run_harbor_task)
    monkeypatch.setattr(RUN.sys, "argv", ["run.py", str(task_path), "--dry-run"])

    assert RUN.main() == 0
    assert [(call["agent"], call["model"]) for call in calls] == [
        ("codex", "gpt-5.5"),
        ("claude-code", "claude-opus-4-8"),
    ]

    captured = capsys.readouterr().out
    assert "Default config mode" in captured


def test_main_groups_multiple_targets_under_auto_batch_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()
    jobs_dir = tmp_path / "jobs"
    config_path = tmp_path / "run-plan.toml"
    config_path.write_text(
        """
schema_version = "harbor-run-plan/v1"

[[runs]]
agent = "codex"
models = ["gpt-5.4", "gpt-5.5"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict[str, object]] = []

    def fake_run_harbor_task(**kwargs):
        calls.append(kwargs)
        return RUN.RunOutcome(exit_code=0)

    monkeypatch.setattr(RUN, "DEFAULT_RUN_CONFIG", config_path)
    monkeypatch.setattr(RUN, "_make_batch_name", lambda task_path: "batch-abc123")
    monkeypatch.setattr(RUN, "run_harbor_task", fake_run_harbor_task)
    monkeypatch.setattr(
        RUN.sys,
        "argv",
        [
            "run.py",
            str(task_path),
            "--jobs-dir",
            str(jobs_dir),
            "--dry-run",
        ],
    )

    assert RUN.main() == 0
    assert [call["jobs_dir"] for call in calls] == [
        jobs_dir / "batch-abc123",
        jobs_dir / "batch-abc123",
    ]

    captured = capsys.readouterr().out
    assert f"Batch directory: {jobs_dir / 'batch-abc123'}" in captured


def test_main_runs_target_combinations_in_parallel_when_combo_concurrent_gt_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()
    jobs_dir = tmp_path / "jobs"
    config_path = tmp_path / "run-plan.toml"
    config_path.write_text(
        """
schema_version = "harbor-run-plan/v1"

[[runs]]
agent = "codex"
models = ["gpt-5.4", "gpt-5.5"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    launched: list[tuple[str, str]] = []
    active = 0
    max_active = 0

    class FakeProcess:
        def __init__(self) -> None:
            self._returned = False

        def poll(self) -> int | None:
            nonlocal active
            if self._returned:
                return 0
            self._returned = True
            active -= 1
            return 0

    def fake_launch(prepared):
        nonlocal active, max_active
        launched.append((prepared.agent, prepared.model))
        active += 1
        max_active = max(max_active, active)
        prepared.process = FakeProcess()

    monkeypatch.setattr(RUN, "DEFAULT_RUN_CONFIG", config_path)
    monkeypatch.setattr(RUN, "_make_batch_name", lambda task_path: "batch-abc123")
    monkeypatch.setattr(RUN, "find_harbor_cli", lambda: "/bin/harbor")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(RUN, "_launch_prepared_run", fake_launch)
    monkeypatch.setattr(RUN, "_load_job_summary", lambda job_dir: None)
    monkeypatch.setattr(RUN, "_try_write_reward_reports", lambda job_dir: [])
    monkeypatch.setattr(RUN, "_supports_live_dashboard", lambda: False)
    monkeypatch.setattr(
        RUN.sys,
        "argv",
        [
            "run.py",
            str(task_path),
            "--jobs-dir",
            str(jobs_dir),
            "--combo-concurrent",
            "2",
        ],
    )

    assert RUN.main() == 0
    assert set(launched) == {("codex", "gpt-5.4"), ("codex", "gpt-5.5")}
    assert max_active == 2

    captured = capsys.readouterr().out
    assert "Combination concurrency: 2" in captured


def test_main_cli_targets_override_default_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()
    jobs_dir = tmp_path / "jobs"
    config_path = tmp_path / "run-plan.toml"
    config_path.write_text(
        """
schema_version = "harbor-run-plan/v1"

[[runs]]
agent = "codex"
models = ["gpt-5.5"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict[str, object]] = []

    def fake_run_harbor_task(**kwargs):
        calls.append(kwargs)
        return RUN.RunOutcome(exit_code=0)

    monkeypatch.setattr(RUN, "DEFAULT_RUN_CONFIG", config_path)
    monkeypatch.setattr(RUN, "_make_batch_name", lambda task_path: "batch-abc123")
    monkeypatch.setattr(RUN, "run_harbor_task", fake_run_harbor_task)
    monkeypatch.setattr(
        RUN.sys,
        "argv",
        [
            "run.py",
            str(task_path),
            "-a",
            "claude-code",
            "-m",
            "claude-opus-4-8",
            "--jobs-dir",
            str(jobs_dir),
            "--dry-run",
        ],
    )

    assert RUN.main() == 0
    assert len(calls) == 1
    assert calls[0]["agent"] == "claude-code"
    assert calls[0]["model"] == "claude-opus-4-8"
    assert calls[0]["jobs_dir"] == jobs_dir / "batch-abc123"

    captured = capsys.readouterr().out
    assert "Default config mode" not in captured
    assert "Single-combination evaluation mode" in captured
    assert f"Batch directory: {jobs_dir / 'batch-abc123'}" in captured


def test_main_falls_back_to_claude_code_and_opus_when_config_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()
    jobs_dir = tmp_path / "jobs"

    calls: list[dict[str, object]] = []

    def fake_run_harbor_task(**kwargs):
        calls.append(kwargs)
        return RUN.RunOutcome(exit_code=0)

    monkeypatch.setattr(RUN, "DEFAULT_RUN_CONFIG", tmp_path / "missing-run-plan.toml")
    monkeypatch.setattr(RUN, "_make_batch_name", lambda task_path: "batch-abc123")
    monkeypatch.setattr(RUN, "run_harbor_task", fake_run_harbor_task)
    monkeypatch.setattr(
        RUN.sys,
        "argv",
        ["run.py", str(task_path), "--jobs-dir", str(jobs_dir), "--dry-run"],
    )

    assert RUN.main() == 0
    assert len(calls) == 1
    assert calls[0]["agent"] == "claude-code"
    assert calls[0]["model"] == "claude-opus-4-8"
    assert calls[0]["jobs_dir"] == jobs_dir / "batch-abc123"

    captured = capsys.readouterr().out
    assert "Single-combination evaluation mode" in captured
    assert f"Batch directory: {jobs_dir / 'batch-abc123'}" in captured


def test_run_harbor_task_dry_run_uses_simplified_job_dir_under_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FixedUUID:
        hex = "abcdef1234567890"

    task_path = tmp_path / "GitGuardian__ggshield__41a2258__947"
    task_path.mkdir()
    jobs_dir = tmp_path / "jobs" / "2026-07-31__12-34-56__GitGuardian__ggshield__41a2258__947__batch__123456"

    monkeypatch.setattr(RUN, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(RUN, "find_harbor_cli", lambda: None)

    outcome = RUN.run_harbor_task(
        task_path=task_path,
        agent="codex",
        model="gpt-5.5",
        jobs_dir=jobs_dir,
        dry_run=True,
    )

    assert outcome.exit_code == 0
    captured = capsys.readouterr().out
    expected_job_dir = jobs_dir / "codex__gpt-5.5__abcdef"
    assert f"Job: {expected_job_dir}" in captured
    assert "2026-07-31__12-34-56__codex__gpt-5.5__abcdef" not in captured
    assert f"--jobs-dir {jobs_dir}" in captured
    assert "--job-name codex__gpt-5.5__abcdef" in captured

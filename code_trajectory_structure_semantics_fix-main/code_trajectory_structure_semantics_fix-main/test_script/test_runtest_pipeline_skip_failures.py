import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from action_semantics_fix_script import runtest_pipeline


class _Board:
    def set_stage(self, *_args) -> None:
        pass

    def note_timing(self, *_args) -> None:
        pass

    def finish(self, *_args) -> None:
        pass

    def skip(self, *_args) -> None:
        pass


def _write_summary(task: Path, outcomes: dict[str, tuple[str, str]]) -> None:
    details = []
    for index, (name, (status, verdict)) in enumerate(outcomes.items(), 1):
        details.append(
            {
                "task_dir": "<ROOT>",
                "status": status,
                "verdict": verdict,
                "reason_codes": (
                    [] if verdict in {"PASS", "NO_TEST_NEEDED"} else ["RUNNER_EXIT_NONZERO"]
                ),
                "source_session": {
                    "source_session_name": name,
                    "source_session_sequence": index,
                    "source_session_type": "develop",
                    "source_session_identifier": Path(name).stem,
                },
            }
        )
    (task / "runtest_summary.json").write_text(
        json.dumps({"report_type": "runtest_summary", "details": details}),
        encoding="utf-8",
    )


class RuntestFailureSkipTests(unittest.TestCase):
    def _task(self, root: Path) -> tuple[Path, Path, Path]:
        task = root / "TASK-TEST"
        sessions = task / "sessions"
        sessions.mkdir(parents=True)
        (task / "metadata.json").write_text(
            json.dumps({"project_type": "server"}), encoding="utf-8"
        )
        good = sessions / "1-develop-good.json"
        failed = sessions / "2-develop-failed.json"
        good.write_text(
            json.dumps({"messages": [{"role": "assistant", "content": "done"}]}),
            encoding="utf-8",
        )
        failed.write_text("{ deliberately invalid but preserved }\n", encoding="utf-8")
        return task, good, failed

    def test_project_type_policy_uses_case_insensitive_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, _good, _failed = self._task(Path(temporary))
            metadata_path = task / "metadata.json"
            for raw in ("server", " WEB ", "FullStack"):
                metadata_path.write_text(
                    json.dumps({"project_type": raw}), encoding="utf-8"
                )
                policy = runtest_pipeline._runtest_project_policy(task)
                self.assertTrue(policy.eligible)
                self.assertEqual(policy.project_type, raw.strip().casefold())

            for payload in (
                {"project_type": "desktop"},
                {"project_type": "mobile"},
                {"project_type": ""},
                {"project_type": ["server"]},
                {},
            ):
                metadata_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(
                    runtest_pipeline._runtest_project_policy(task).eligible
                )

            metadata_path.write_text("{ invalid json", encoding="utf-8")
            self.assertFalse(runtest_pipeline._runtest_project_policy(task).eligible)
            metadata_path.unlink()
            self.assertFalse(runtest_pipeline._runtest_project_policy(task).eligible)

    def test_ineligible_project_type_skips_task_before_any_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task, good, failed = self._task(base)
            (task / "metadata.json").write_text(
                json.dumps({"project_type": "desktop"}), encoding="utf-8"
            )
            before = {path.name: path.read_bytes() for path in (good, failed)}
            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                ["--root", str(task), "--python", sys.executable]
            )
            log_dir = base / "logs"
            log_dir.mkdir()
            board = _Board()

            with (
                mock.patch.object(
                    runtest_pipeline,
                    "_run_captured",
                    side_effect=AssertionError("child process must not run"),
                ),
                mock.patch.object(runtest_pipeline, "_write_task_chainlog"),
            ):
                result = runtest_pipeline._run_task_pipeline(
                    task,
                    args,
                    board,
                    log_dir,
                    base / "failed",
                )

            self.assertEqual(result["final_rc"], 0)
            self.assertTrue(result["skipped"])
            self.assertEqual(result["skip_code"], "project_type_not_eligible")
            self.assertEqual(result["project_type"], "desktop")
            self.assertEqual(
                {path.name: path.read_bytes() for path in (good, failed)}, before
            )

            with mock.patch.object(
                runtest_pipeline,
                "run_step",
                side_effect=AssertionError("child process must not run"),
            ):
                self.assertEqual(runtest_pipeline._chain_main_sequential(args), 0)

    def test_parallel_chain_skips_all_ineligible_tasks_before_docker_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task, _good, _failed = self._task(base)
            (task / "metadata.json").write_text(
                json.dumps({"project_type": "desktop"}), encoding="utf-8"
            )
            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                ["--root", str(base), "--python", sys.executable]
            )

            with (
                mock.patch.object(
                    runtest_pipeline,
                    "check_docker_preflight",
                    side_effect=AssertionError("Docker preflight must not run"),
                ),
                mock.patch.object(
                    runtest_pipeline,
                    "_run_captured",
                    side_effect=AssertionError("child process must not run"),
                ),
                mock.patch.object(runtest_pipeline, "_write_task_chainlog"),
                mock.patch("sys.stdout", new=io.StringIO()),
            ):
                self.assertEqual(runtest_pipeline._chain_main_parallel(args), 0)

    def test_mixed_project_types_run_only_eligible_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            desktop, _good, _failed = self._task(base)
            desktop.rename(base / "TASK-DESKTOP")
            desktop = base / "TASK-DESKTOP"
            (desktop / "metadata.json").write_text(
                json.dumps({"project_type": "desktop"}), encoding="utf-8"
            )

            server = base / "TASK-SERVER"
            (server / "sessions").mkdir(parents=True)
            (server / "metadata.json").write_text(
                json.dumps({"project_type": "server"}), encoding="utf-8"
            )
            (server / "sessions" / "1-develop.json").write_text(
                json.dumps({"messages": []}), encoding="utf-8"
            )
            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                ["--root", str(base), "--python", sys.executable, "--no-docker-preflight"]
            )
            processed: list[str] = []
            original_task_pipeline = runtest_pipeline._run_task_pipeline

            def fake_task_pipeline(task_dir, *task_args):
                if not runtest_pipeline._runtest_project_policy(task_dir).eligible:
                    return original_task_pipeline(task_dir, *task_args)
                processed.append(task_dir.name)
                return {
                    "name": task_dir.name,
                    "final_rc": 0,
                    "fail_stage": None,
                    "gate_failures": 0,
                }

            with (
                mock.patch.object(
                    runtest_pipeline,
                    "_run_task_pipeline",
                    side_effect=fake_task_pipeline,
                ),
                mock.patch("sys.stdout", new=io.StringIO()),
            ):
                self.assertEqual(runtest_pipeline._chain_main_parallel(args), 0)

            self.assertEqual(processed, ["TASK-SERVER"])
            self.assertFalse(runtest_pipeline._runtest_project_policy(desktop).eligible)
            self.assertTrue(runtest_pipeline._runtest_project_policy(server).eligible)

    def test_direct_runtests_command_applies_project_type_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, _good, _failed = self._task(Path(temporary))
            (task / "metadata.json").write_text(
                json.dumps({"project_type": "desktop"}), encoding="utf-8"
            )
            repo = task / "repo"
            repo.mkdir()
            (repo / "run_tests.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            with (
                mock.patch.object(
                    runtest_pipeline,
                    "check_docker_preflight",
                    side_effect=AssertionError("Docker preflight must not run"),
                ),
                mock.patch.object(
                    runtest_pipeline,
                    "run_claude_for_repo",
                    side_effect=AssertionError("Claude must not run"),
                ),
                mock.patch("sys.stdout", new=io.StringIO()),
            ):
                rc = runtest_pipeline.runtests_main(["--root", str(task)])

            self.assertEqual(rc, 0)
            self.assertFalse((task / "runtest_summary.json").exists())

    def test_cleaner_and_addtool_preserve_failed_session_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, _good, failed = self._task(Path(temporary))
            _write_summary(
                task,
                {
                    "1-develop-good.json": ("ok", "PASS"),
                    "2-develop-failed.json": ("failed", "TEST_FAILED_UNRESOLVED"),
                },
            )
            before = failed.read_bytes()

            clean_report = task / "clean.json"
            rc = runtest_pipeline.clean_main(
                [
                    "--root",
                    str(task),
                    "--action",
                    "delete",
                    "--only-synthetic",
                    "--preserve-step1-failures",
                    "--report",
                    str(clean_report),
                ]
            )
            self.assertEqual(rc, 0)
            clean_payload = json.loads(clean_report.read_text(encoding="utf-8"))
            self.assertEqual(clean_payload["summary"]["files_scanned"], 1)
            self.assertEqual(clean_payload["summary"]["failed_sessions_preserved"], 1)
            self.assertEqual(failed.read_bytes(), before)

            report = runtest_pipeline.addtool_build_report(
                scan_root=task,
                task_root=task,
                llm_enabled=False,
            )
            runtest_pipeline.process_session_file(
                failed,
                report_root_override=None,
                config=None,
                report=report,
                failed_step1_sessions=runtest_pipeline._failed_step1_session_keys(task),
            )
            self.assertEqual(
                report["files"][0]["status"],
                "skipped_runtest_failed_unchanged",
            )
            self.assertTrue(report["files"][0]["source_preserved"])
            self.assertEqual(failed.read_bytes(), before)

    def test_mixed_session_failure_does_not_fail_task_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task, _good, failed = self._task(base)
            _write_summary(
                task,
                {
                    "1-develop-good.json": ("ok", "PASS"),
                    "2-develop-failed.json": ("failed", "TEST_FAILED_UNRESOLVED"),
                },
            )
            before = failed.read_bytes()
            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                ["--root", str(task), "--python", sys.executable]
            )
            commands: list[list[str]] = []

            def fake_run(cmd, _logf, _board, _name, **_kwargs):
                commands.append(list(cmd))
                return 1 if "runtests" in cmd else 0

            log_dir = base / "logs"
            log_dir.mkdir()
            with (
                mock.patch.object(runtest_pipeline, "_run_captured", side_effect=fake_run),
                mock.patch.object(runtest_pipeline, "summarize_add_tool", return_value=0),
                mock.patch.object(runtest_pipeline, "_write_task_chainlog"),
            ):
                result = runtest_pipeline._run_task_pipeline(
                    task,
                    args,
                    _Board(),
                    log_dir,
                    base / "failed",
                )

            self.assertEqual(result["final_rc"], 0)
            self.assertEqual(
                result["skipped_sessions"],
                ["TASK-TEST/2-develop-failed.json"],
            )
            self.assertEqual(failed.read_bytes(), before)
            manifest = base / "failed" / "TASK-TEST" / "failure_manifest.json"
            self.assertTrue(manifest.is_file())
            for cmd in commands[1:]:
                self.assertIn("--preserve-step1-failures", cmd)

    def test_all_session_failures_do_not_fail_task_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task, good, failed = self._task(base)
            _write_summary(
                task,
                {
                    "1-develop-good.json": ("failed", "TEST_FAILED_UNRESOLVED"),
                    "2-develop-failed.json": ("failed", "TEST_FAILED_UNRESOLVED"),
                },
            )
            before = {path.name: path.read_bytes() for path in (good, failed)}

            with mock.patch.object(
                sys,
                "argv",
                [
                    "addtool",
                    "--root",
                    str(task),
                    "--preserve-step1-failures",
                ],
            ):
                self.assertEqual(runtest_pipeline.addtool_main(), 0)
            self.assertEqual(
                {path.name: path.read_bytes() for path in (good, failed)}, before
            )

            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                ["--root", str(task), "--python", sys.executable]
            )
            log_dir = base / "logs"
            log_dir.mkdir()

            def fake_run(cmd, _logf, _board, _name, **_kwargs):
                return 1 if "runtests" in cmd else 0

            with (
                mock.patch.object(runtest_pipeline, "_run_captured", side_effect=fake_run),
                mock.patch.object(runtest_pipeline, "summarize_add_tool", return_value=0),
                mock.patch.object(runtest_pipeline, "_write_task_chainlog"),
            ):
                result = runtest_pipeline._run_task_pipeline(
                    task,
                    args,
                    _Board(),
                    log_dir,
                    base / "failed",
                )

            self.assertEqual(result["final_rc"], 0)
            self.assertEqual(
                result["skipped_sessions"],
                [
                    "TASK-TEST/1-develop-good.json",
                    "TASK-TEST/2-develop-failed.json",
                ],
            )
            self.assertEqual(
                {path.name: path.read_bytes() for path in (good, failed)}, before
            )
            manifest = base / "failed" / "TASK-TEST" / "failure_manifest.json"
            self.assertTrue(manifest.is_file())

    def test_sequential_chain_skips_failed_session_without_task_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task, good, failed = self._task(base)
            _write_summary(
                task,
                {
                    "1-develop-good.json": ("ok", "PASS"),
                    "2-develop-failed.json": ("failed", "TEST_FAILED_UNRESOLVED"),
                },
            )
            before = {path.name: path.read_bytes() for path in (good, failed)}
            failed_root = base / "failed"
            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                [
                    "--root",
                    str(task),
                    "--python",
                    sys.executable,
                    "--failed-root",
                    str(failed_root),
                ]
            )
            commands: list[list[str]] = []

            def fake_run_step(_title, cmd):
                commands.append(list(cmd))
                return 1 if "runtests" in cmd else 0

            with (
                mock.patch.object(runtest_pipeline, "run_step", side_effect=fake_run_step),
                mock.patch.object(runtest_pipeline, "summarize_add_tool", return_value=0),
            ):
                rc = runtest_pipeline._chain_main_sequential(args)

            self.assertEqual(rc, 0)
            self.assertEqual(
                {path.name: path.read_bytes() for path in (good, failed)}, before
            )
            self.assertTrue(
                (failed_root / "TASK-TEST" / "failure_manifest.json").is_file()
            )
            for cmd in commands[1:]:
                self.assertIn("--preserve-step1-failures", cmd)

    def test_incomplete_step1_summary_remains_a_task_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task, _good, _failed = self._task(base)
            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                ["--root", str(task), "--python", sys.executable]
            )
            log_dir = base / "logs"
            log_dir.mkdir()
            commands: list[list[str]] = []

            def fake_run(cmd, _logf, _board, _name, **_kwargs):
                commands.append(list(cmd))
                return 1

            with (
                mock.patch.object(runtest_pipeline, "_run_captured", side_effect=fake_run),
                mock.patch.object(runtest_pipeline, "_write_task_chainlog"),
            ):
                result = runtest_pipeline._run_task_pipeline(
                    task,
                    args,
                    _Board(),
                    log_dir,
                    base / "failed",
                )

            self.assertEqual(result["final_rc"], 1)
            self.assertEqual(result["fail_stage"], "① 测试检测/结果不完整")
            self.assertEqual(len(commands), 1)

    def test_step1_process_error_remains_a_task_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task, _good, _failed = self._task(base)
            _write_summary(
                task,
                {
                    "1-develop-good.json": ("ok", "PASS"),
                    "2-develop-failed.json": ("failed", "TEST_FAILED_UNRESOLVED"),
                },
            )
            args = runtest_pipeline.chain_build_arg_parser().parse_args(
                ["--root", str(task), "--python", sys.executable]
            )
            log_dir = base / "logs"
            log_dir.mkdir()
            commands: list[list[str]] = []

            def fake_run(cmd, _logf, _board, _name, **_kwargs):
                commands.append(list(cmd))
                return 2

            with (
                mock.patch.object(runtest_pipeline, "_run_captured", side_effect=fake_run),
                mock.patch.object(runtest_pipeline, "_write_task_chainlog"),
            ):
                result = runtest_pipeline._run_task_pipeline(
                    task,
                    args,
                    _Board(),
                    log_dir,
                    base / "failed",
                )

            self.assertEqual(result["final_rc"], 2)
            self.assertEqual(result["fail_stage"], "① 测试检测")
            self.assertEqual(len(commands), 1)


if __name__ == "__main__":
    unittest.main()

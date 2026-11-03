from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import action_semantics_fix
import full_pipeline


class FullPipelineTests(unittest.TestCase):
    def make_task_root(self, base: Path, session_count: int = 2) -> tuple[Path, Path, list[Path]]:
        root = base / "tasks"
        task = root / "TASK-TEST-001"
        sessions = task / "sessions"
        sessions.mkdir(parents=True)
        files = []
        for index in range(session_count):
            path = sessions / f"{index + 1}-session.json"
            path.write_text('{"messages": []}\n', encoding="utf-8")
            files.append(path.resolve())
        return root, task, files

    def write_operator(
        self,
        root: Path,
        *,
        operator_code: str = "qa-1",
        operator_name: str = "测试用户",
    ) -> None:
        (root / "operator.json").write_text(
            json.dumps(
                {
                    "operator_code": operator_code,
                    "operator_name": operator_name,
                }
            ),
            encoding="utf-8",
        )

    def test_cli_defaults_and_count_alias(self) -> None:
        local_args = full_pipeline.parse_args([])
        platform_args = full_pipeline.parse_args(["--count", "3"])

        self.assertEqual(local_args.root, full_pipeline.ROOT_DIR)
        self.assertEqual(local_args.config, full_pipeline.DEFAULT_CONFIG_PATH)
        self.assertIsNone(local_args.task_count)
        self.assertEqual(platform_args.task_count, 3)

    def test_load_root_task_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "taskid.txt").write_text(
                "TASK-TEST-001\nTASK-TEST-002, TASK-TEST-003\n",
                encoding="utf-8",
            )
            (root / "operator.json").write_text(
                json.dumps({"user_id": "user-7", "user_name": "测试用户"}),
                encoding="utf-8",
            )

            manifest = full_pipeline.load_root_task_manifest(root)

            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(
                manifest.task_ids,
                ["TASK-TEST-001", "TASK-TEST-002", "TASK-TEST-003"],
            )
            self.assertEqual(manifest.user_id, "user-7")
            self.assertEqual(manifest.user_name, "测试用户")

    def test_root_manifest_drives_task_selection_and_pauses_platform_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, task, _sessions = self.make_task_root(base, session_count=1)
            (root / "tasks.txt").write_text(f"{task.name}\n", encoding="utf-8")
            (root / "operator.json").write_text(
                json.dumps({"operator_code": "user-7", "operator_name": "测试用户"}),
                encoding="utf-8",
            )
            action = base / "action.py"
            structure = base / "structure.py"
            action.write_text("", encoding="utf-8")
            structure.write_text("", encoding="utf-8")
            config = base / "pipeline.toml"
            config.write_text(
                "config_version = 1\n"
                "[general]\n"
                "cleanup_task_dirs_on_success = false\n"
                "[database]\n"
                "enabled = false\n"
                "[judge]\n"
                "enabled = false\n"
                "[runtest]\n"
                "enabled = false\n"
                "[hooks.claim_tasks]\n"
                "enabled = true\n"
                'command = ["python", "missing-claim.py"]\n'
                "[hooks.mark_qa_complete]\n"
                "enabled = true\n"
                'command = ["python", "missing-complete.py"]\n',
                encoding="utf-8",
            )

            rc = full_pipeline.main([
                "-r", str(root),
                "--config", str(config),
                "--run-dir", str(base / "run"),
                "--action-entry", str(action),
                "--structure-entry", str(structure),
            ])

            self.assertEqual(rc, 0)
            report = json.loads(
                (base / "run" / "full_pipeline_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["task_ids"], [task.name])
            stages = {stage["name"]: stage for stage in report["stages"]}
            self.assertEqual(stages["claim_tasks"]["status"], "skipped")
            self.assertEqual(stages["mark_qa_complete"]["status"], "skipped")
            context = json.loads(
                (base / "run" / "pipeline_context.json").read_text(encoding="utf-8")
            )
            self.assertEqual(context["user_id"], "user-7")
            self.assertEqual(context["user_name"], "测试用户")
            self.assertEqual(context["operator_code"], "user-7")

    def test_local_manifest_continues_when_optional_database_is_unavailable(self) -> None:
        class FlakyDatabase:
            instance = None

            def __init__(self, _settings) -> None:
                self.calls: list[str] = []
                self.failures_remaining = 1
                FlakyDatabase.instance = self

            def ensure_task_rows(self, *_args, **_kwargs) -> None:
                self.calls.append("ensure_task_rows")
                if self.failures_remaining:
                    self.failures_remaining -= 1
                    raise TimeoutError("database down")

            def mark_running(self, _task_ids) -> None:
                self.calls.append("mark_running")

            def mark_stage_flag(self, _task_ids, _column, _value) -> None:
                self.calls.append("mark_stage_flag")

            def set_task_sub_status(self, _task_ids, _status, _status_name) -> None:
                self.calls.append("set_task_sub_status")

            def mark_finished(self, _task_ids) -> None:
                self.calls.append("mark_finished")

            def mark_completed(self, _task_ids) -> None:
                self.calls.append("mark_completed")

            def close(self) -> None:
                self.calls.append("close")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, task, _sessions = self.make_task_root(base, session_count=1)
            (root / "tasks.txt").write_text(f"{task.name}\n", encoding="utf-8")
            self.write_operator(root)
            action = base / "action.py"
            structure = base / "structure.py"
            action.write_text("", encoding="utf-8")
            structure.write_text("", encoding="utf-8")
            config = base / "pipeline.toml"
            config.write_text(
                "config_version = 1\n"
                "[general]\n"
                "cleanup_task_dirs_on_success = false\n"
                "[database]\n"
                "enabled = true\n"
                'host = "127.0.0.1"\n'
                'database = "demo"\n'
                'user = "demo"\n'
                'password = "secret"\n'
                "[judge]\n"
                "enabled = false\n"
                "[runtest]\n"
                "enabled = false\n"
                "[hooks.fetch_from_oss]\n"
                "enabled = false\n",
                encoding="utf-8",
            )

            original_database = full_pipeline.TaskStructureSemanticsDatabase
            full_pipeline.TaskStructureSemanticsDatabase = FlakyDatabase
            try:
                rc = full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--action-entry", str(action),
                    "--structure-entry", str(structure),
                ])
            finally:
                full_pipeline.TaskStructureSemanticsDatabase = original_database

            self.assertEqual(rc, 0)
            context = json.loads(
                (base / "run" / "pipeline_context.json").read_text(encoding="utf-8")
            )
            self.assertIn("database unavailable", context["database_warning"])
            calls = FlakyDatabase.instance.calls
            self.assertLess(calls.index("close"), calls.index("mark_running"))
            self.assertIn("set_task_sub_status", calls)
            self.assertIn("mark_completed", calls)

    def test_resume_snapshot_rewinds_the_interrupted_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run_dir = base / "pipeline_runs" / "run-1"
            run_dir.mkdir(parents=True)
            after_path = base / "after" / "TASK-TEST-001"
            state = full_pipeline.TaskState(
                "TASK-TEST-001",
                str(base / "success_task" / "TASK-TEST-001"),
                status="failed",
                failed_stage="action_semantics_by_session",
                error="interrupted",
                stages={
                    "claim_tasks": "completed",
                    "fetch_from_oss": "completed",
                    "action_semantics_by_session": "failed",
                },
                artifacts={
                    "fetch_from_oss": {"after_task_path": str(after_path)},
                    "task_bucket": "success_task",
                    "bucket_task_path": str(base / "success_task" / "TASK-TEST-001"),
                },
            )
            def stage_payload(name: str, status: str) -> dict[str, object]:
                return {
                    "name": name,
                    "status": status,
                    "started_at": "",
                    "finished_at": "",
                    "elapsed_seconds": 0,
                    "items": [],
                    "summary": {},
                    "reason": "",
                }
            report = {
                "status": "interrupted",
                "config_path": str(base / "pipeline.toml"),
                "stages": [
                    stage_payload("claim_tasks", "completed"),
                    stage_payload("fetch_from_oss", "completed"),
                    stage_payload("action_semantics_by_session", "partial"),
                ],
                "task_states": {state.task_id: full_pipeline.asdict(state)},
            }
            context = {
                "root": str(base / "tasks"),
                "run_dir": str(run_dir),
                "claim_requested": False,
                "task_states": {state.task_id: full_pipeline.asdict(state)},
                "resume_options": {"claim_requested": False},
            }
            (run_dir / "full_pipeline_report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (run_dir / "pipeline_context.json").write_text(
                json.dumps(context), encoding="utf-8"
            )

            snapshot = full_pipeline.load_resume_snapshot(run_dir)

            self.assertEqual(snapshot.resume_index, 2)
            self.assertEqual(set(snapshot.stages), {"claim_tasks", "fetch_from_oss"})
            resumed_state = snapshot.states[state.task_id]
            self.assertEqual(resumed_state.status, "pending")
            self.assertEqual(resumed_state.task_path, str(after_path.resolve()))
            self.assertIn("fetch_from_oss", resumed_state.artifacts)
            self.assertNotIn("task_bucket", resumed_state.artifacts)

    def test_resume_skips_completed_stages_and_restarts_from_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, _sessions = self.make_task_root(base, session_count=1)
            run_dir = base / "pipeline_runs" / "run-1"
            action_marker = base / "action-count.txt"
            structure_marker = base / "structure-count.txt"
            worker_source = '''
from pathlib import Path

marker = Path(r"{marker}")
count = int(marker.read_text(encoding="utf-8")) if marker.exists() else 0
marker.write_text(str(count + 1), encoding="utf-8")
'''
            action_entry = base / "action.py"
            structure_entry = base / "structure.py"
            action_entry.write_text(
                worker_source.format(marker=action_marker), encoding="utf-8"
            )
            structure_entry.write_text(
                worker_source.format(marker=structure_marker), encoding="utf-8"
            )
            config = base / "pipeline.toml"
            config.write_text(
                "config_version = 1\n"
                "[general]\n"
                "cleanup_task_dirs_on_success = false\n"
                "[database]\n"
                "enabled = false\n"
                "[judge]\n"
                "enabled = false\n"
                "[runtest]\n"
                "enabled = false\n",
                encoding="utf-8",
            )

            rc = full_pipeline.main([
                "-r", str(root),
                "--config", str(config),
                "--run-dir", str(run_dir),
                "--action-entry", str(action_entry),
                "--structure-entry", str(structure_entry),
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(action_marker.read_text(encoding="utf-8"), "1")
            self.assertEqual(structure_marker.read_text(encoding="utf-8"), "1")

            report_path = run_dir / "full_pipeline_report.json"
            context_path = run_dir / "pipeline_context.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["status"] = "interrupted"
            report["stages"] = report["stages"][:3]
            report_path.write_text(json.dumps(report), encoding="utf-8")
            context = json.loads(context_path.read_text(encoding="utf-8"))
            context_path.write_text(json.dumps(context), encoding="utf-8")

            resumed_rc = full_pipeline.main([
                "-r", str(root),
                "--config", str(config),
                "--run-dir", str(run_dir),
                "--resume",
            ])

            self.assertEqual(resumed_rc, 0)
            self.assertEqual(action_marker.read_text(encoding="utf-8"), "1")
            self.assertEqual(structure_marker.read_text(encoding="utf-8"), "2")
            resumed_report = json.loads(
                (run_dir / "full_pipeline_report.json").read_text(encoding="utf-8")
            )
            first_three = resumed_report["stages"][:3]
            self.assertTrue(
                all(stage["summary"].get("resumed_from_checkpoint") for stage in first_three)
            )

    def test_action_scheduler_targets_exact_session_for_every_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _root, task, sessions = self.make_task_root(base, session_count=2)
            report_root = base / "action-reports"
            rc = action_semantics_fix.main([
                "-r", str(task),
                "--session-file", str(sessions[1]),
                "--report-dir", str(report_root),
                "--dry-run",
                "--quiet",
            ])
            self.assertEqual(rc, 0)
            reports = list(report_root.glob("*/action_semantics_fix_report.json"))
            self.assertEqual(len(reports), 1)
            payload = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["session_file"], str(sessions[1]))
            records = [item for stage in payload["steps"] for item in stage["tasks"]]
            self.assertEqual(len(records), 5)
            for record in records:
                self.assertIn(str(sessions[1]), record["command"])
                self.assertNotIn(str(sessions[0]), record["command"])
            self.assertEqual({record["step"] for record in records}, {
                "remove_meaningless_dialogues",
                "modify_assistant_content",
                "fix_missing_summary_nodes",
                "clean_last_assistant",
                "ua_detect_and_repair",
            })

    def test_full_pipeline_dry_run_has_required_stage_order_and_granularity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, sessions = self.make_task_root(base, session_count=2)
            run_dir = base / "dry-run"
            rc = full_pipeline.main([
                "-r", str(root),
                "--run-dir", str(run_dir),
                "--skip", ",".join(sorted(full_pipeline.HOOK_STAGES)),
                "--dry-run",
            ])
            self.assertEqual(rc, 0)
            payload = json.loads((run_dir / "full_pipeline_report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "planned")
            self.assertEqual([stage["name"] for stage in payload["stages"]], list(full_pipeline.STAGE_ORDER))
            by_name = {stage["name"]: stage for stage in payload["stages"]}
            self.assertEqual(by_name["action_semantics_by_session"]["summary"]["total"], 2)
            self.assertEqual(by_name["runtest_pipeline_by_task"]["status"], "planned")
            self.assertEqual(by_name["runtest_pipeline_by_task"]["summary"]["total"], 1)
            self.assertEqual(by_name["runtest_pipeline_by_task"]["summary"]["planned"], 1)
            self.assertEqual(by_name["structure_check_fix_by_task"]["summary"]["total"], 1)
            self.assertEqual(by_name["quality_judge"]["status"], "skipped")
            self.assertEqual(by_name["quality_judge"]["reason"], "judge.enabled=false")
            action_commands = [item["command"] for item in by_name["action_semantics_by_session"]["items"]]
            for session in sessions:
                self.assertTrue(any(str(session) in command for command in action_commands))
            for hook in full_pipeline.HOOK_STAGES:
                self.assertEqual(by_name[hook]["status"], "skipped")

    def test_overlay_preserves_original_for_excluded_action_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            full_task = base / "full" / "TASK-TEST-001"
            action_task = base / "action" / "TASK-TEST-001"
            (full_task / "sessions").mkdir(parents=True)
            (action_task / "sessions").mkdir(parents=True)
            (full_task / "sessions" / "kept.json").write_text("original-kept\n", encoding="utf-8")
            (full_task / "sessions" / "excluded.json").write_text("original-excluded\n", encoding="utf-8")
            (action_task / "sessions" / "kept.json").write_text("repaired-kept\n", encoding="utf-8")
            skipped: list[str] = []

            written = full_pipeline.overlay_prepared_sessions(
                full_task,
                action_task,
                [
                    {"relative_path": "kept.json"},
                    {"relative_path": "excluded.json"},
                ],
                skip_missing=True,
                skipped_missing=skipped,
            )

            self.assertEqual(written, ["kept.json"])
            self.assertEqual(skipped, ["excluded.json"])
            self.assertEqual((full_task / "sessions" / "kept.json").read_text(encoding="utf-8"), "repaired-kept\n")
            self.assertEqual((full_task / "sessions" / "excluded.json").read_text(encoding="utf-8"), "original-excluded\n")

    def test_disabled_judge_is_not_validated_or_required_for_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, task, _sessions = self.make_task_root(base, session_count=1)
            action = base / "action.py"
            structure = base / "structure.py"
            action.write_text("", encoding="utf-8")
            structure.write_text("", encoding="utf-8")
            config = base / "pipeline.toml"
            config.write_text(
                "[judge]\n"
                "enabled = false\n"
                'entry = "missing-judge.py"\n',
                encoding="utf-8",
            )

            rc = full_pipeline.main([
                "-r", str(root),
                "--config", str(config),
                "--run-dir", str(base / "run"),
                "--action-entry", str(action),
                "--structure-entry", str(structure),
                "--skip", ",".join(sorted(full_pipeline.HOOK_STAGES)),
            ])

            self.assertEqual(rc, 0)
            report = json.loads((base / "run" / "full_pipeline_report.json").read_text(encoding="utf-8"))
            by_name = {stage["name"]: stage for stage in report["stages"]}
            self.assertEqual(by_name["quality_judge"]["status"], "skipped")
            self.assertEqual(by_name["quality_judge"]["reason"], "judge.enabled=false")
            self.assertEqual(report["task_states"][task.name]["status"], "completed")
            self.assertFalse((base / "run" / "quality" / "grades.jsonl").exists())

    def test_local_prepare_ignores_ua_excluded_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, task, _sessions = self.make_task_root(base, session_count=1)
            action_task = base / "action" / task.name
            (action_task / "sessions" / "ua_excluded").mkdir(parents=True)
            (action_task / "sessions" / "1-session.json").write_text('{"messages": []}\n', encoding="utf-8")
            (action_task / "sessions" / "ua_excluded" / "excluded.json").write_text(
                '{"messages": []}\n', encoding="utf-8"
            )
            state = full_pipeline.TaskState(task.name, str(task))

            stage = full_pipeline.prepare_local_action_inputs(
                {task.name: state},
                base / "action",
                "**/*.json",
                False,
            )

            self.assertEqual(stage.status, "completed")
            artifact = state.artifacts["fetch_from_oss"]
            self.assertEqual(artifact["replacement_count"], 1)
            self.assertEqual(artifact["replacements"][0]["relative_path"], "1-session.json")

    def test_full_pipeline_enforces_barriers_with_fake_entries(self) -> None:
        fake_source = '''
import json
import os
import sys
import time
from pathlib import Path

kind = Path(__file__).stem
log_dir = Path(os.environ["FAKE_PIPELINE_LOG_DIR"])
log_dir.mkdir(parents=True, exist_ok=True)
record = log_dir / f"{kind}-{os.getpid()}.json"
record.write_text(json.dumps({"kind": kind, "args": sys.argv[1:], "time_ns": time.time_ns()}), encoding="utf-8")
if kind == "judge":
    out = Path(sys.argv[sys.argv.index("--merged-out") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    task = Path(sys.argv[sys.argv.index("--task") + 1])
    sessions = sorted((task / "sessions").rglob("*.json"))
    out.write_text("".join(json.dumps({"task_id": task.name, "session_id": path.stem}) + "\\n" for path in sessions), encoding="utf-8")
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, _sessions = self.make_task_root(base, session_count=3)
            entries = {}
            for name in ("action", "structure", "judge"):
                path = base / f"{name}.py"
                path.write_text(fake_source, encoding="utf-8")
                entries[name] = path
            config = base / "pipeline.toml"
            config.write_text("[judge]\nenabled = true\n", encoding="utf-8")
            call_log_dir = base / "calls"
            old_value = os.environ.get("FAKE_PIPELINE_LOG_DIR")
            os.environ["FAKE_PIPELINE_LOG_DIR"] = str(call_log_dir)
            try:
                rc = full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--action-entry", str(entries["action"]),
                    "--structure-entry", str(entries["structure"]),
                    "--judge-entry", str(entries["judge"]),
                    "--action-session-workers", "3",
                    "--skip", ",".join(sorted(full_pipeline.HOOK_STAGES)),
                ])
            finally:
                if old_value is None:
                    os.environ.pop("FAKE_PIPELINE_LOG_DIR", None)
                else:
                    os.environ["FAKE_PIPELINE_LOG_DIR"] = old_value
            self.assertEqual(rc, 0)
            calls = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in call_log_dir.glob("*.json")
            ]
            kinds = [call["kind"] for call in calls]
            self.assertEqual(kinds.count("action"), 3)
            self.assertEqual(kinds.count("structure"), 1)
            self.assertEqual(kinds.count("judge"), 1)
            last_action = max(call["time_ns"] for call in calls if call["kind"] == "action")
            structure_time = next(call["time_ns"] for call in calls if call["kind"] == "structure")
            judge_time = next(call["time_ns"] for call in calls if call["kind"] == "judge")
            self.assertLess(last_action, judge_time)
            self.assertLess(judge_time, structure_time)
            self.assertTrue((base / "run" / "quality" / "grades.jsonl").is_file())

    def test_prepared_success_sessions_are_the_only_action_inputs_and_overlay_before_structure(self) -> None:
        hook_source = '''
import json
import os
from pathlib import Path

context = json.loads(Path(os.environ["PIPELINE_CONTEXT_PATH"]).read_text(encoding="utf-8"))
task_id = context["task_ids"][0]
action_task = Path(os.environ["FAKE_ACTION_TASK_PATH"])
session = action_task / "sessions" / "1-session.json"
payload = {
    "operation": "prepare",
    "results": [{
        "task_id": task_id,
        "status": "prepared",
        "action_task_path": str(action_task),
        "action_sessions": [str(session)],
        "replacements": [{"session_id": "one", "relative_path": "1-session.json"}],
    }],
}
result = Path(os.environ["PIPELINE_HOOK_RESULT_PATH"])
result.parent.mkdir(parents=True, exist_ok=True)
result.write_text(json.dumps(payload), encoding="utf-8")
'''
        action_source = '''
import sys
from pathlib import Path

session = Path(sys.argv[sys.argv.index("--session-file") + 1])
session.write_text("action-repaired\\n", encoding="utf-8")
'''
        structure_source = '''
import json
import os
import sys
from pathlib import Path

task = Path(sys.argv[sys.argv.index("-r") + 1])
sessions = task / "sessions"
snapshot = {
    "one": (sessions / "1-session.json").read_text(encoding="utf-8"),
    "two": (sessions / "2-session.json").read_text(encoding="utf-8"),
    "runtest_complete": Path(os.environ["FAKE_RUNTEST_SNAPSHOT"]).is_file(),
}
Path(os.environ["FAKE_STRUCTURE_SNAPSHOT"]).write_text(json.dumps(snapshot), encoding="utf-8")
'''
        runtest_source = '''
import json
import os
import sys
from pathlib import Path

task = Path(sys.argv[sys.argv.index("--root") + 1])
sessions = sorted((task / "sessions").glob("*.json"))
Path(os.environ["FAKE_RUNTEST_SNAPSHOT"]).write_text(
    json.dumps({p.name: p.read_text(encoding="utf-8") for p in sessions}),
    encoding="utf-8",
)
'''
        judge_source = '''
import json
import os
import sys
from pathlib import Path

task = Path(sys.argv[sys.argv.index("--task") + 1])
out = Path(sys.argv[sys.argv.index("--merged-out") + 1])
sessions = sorted((task / "sessions").rglob("*.json"))
Path(os.environ["FAKE_JUDGE_SNAPSHOT"]).write_text(
    json.dumps({p.name: p.read_text(encoding="utf-8") for p in sessions}),
    encoding="utf-8",
)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("".join(json.dumps({"task_id": task.name, "session_id": p.stem}) + "\\n" for p in sessions), encoding="utf-8")
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, sessions = self.make_task_root(base, session_count=2)
            sessions[0].write_text("original-one\n", encoding="utf-8")
            sessions[1].write_text("original-two\n", encoding="utf-8")
            action_task = base / "action-input" / "TASK-TEST-001"
            (action_task / "sessions").mkdir(parents=True)
            (action_task / "sessions" / "1-session.json").write_text("repair-success\n", encoding="utf-8")
            hook = base / "prepare_hook.py"
            hook.write_text(hook_source, encoding="utf-8")
            action = base / "action.py"
            action.write_text(action_source, encoding="utf-8")
            runtest = base / "runtest.py"
            runtest.write_text(runtest_source, encoding="utf-8")
            structure = base / "structure.py"
            structure.write_text(structure_source, encoding="utf-8")
            judge = base / "judge.py"
            judge.write_text(judge_source, encoding="utf-8")
            config = base / "pipeline.toml"
            config.write_text(
                "\n".join([
                    "config_version = 1",
                    "[general]",
                    'task_glob = "TASK-*"',
                    'session_glob = "**/*.json"',
                    "action_session_workers = 2",
                    "runtest_task_workers = 1",
                    "structure_task_workers = 1",
                    "judge_session_workers = 1",
                    "[action]",
                    "workers = 1",
                    "extra_args = []",
                    "[runtest]",
                    "enabled = true",
                    "workers = 1",
                    "extra_args = []",
                    "[structure]",
                    "check_workers = 1",
                    "fix_workers = 1",
                    "extra_args = []",
                    "[judge]",
                    "enabled = true",
                    "extra_args = []",
                    "[hooks.fetch_from_oss]",
                    "enabled = true",
                    f"command = {json.dumps([sys.executable, str(hook)])}",
                ]) + "\n",
                encoding="utf-8",
            )
            snapshot_path = base / "structure-snapshot.json"
            runtest_snapshot_path = base / "runtest-snapshot.json"
            judge_snapshot_path = base / "judge-snapshot.json"
            env_values = {
                "FAKE_ACTION_TASK_PATH": str(action_task),
                "FAKE_RUNTEST_SNAPSHOT": str(runtest_snapshot_path),
                "FAKE_STRUCTURE_SNAPSHOT": str(snapshot_path),
                "FAKE_JUDGE_SNAPSHOT": str(judge_snapshot_path),
            }
            previous = {key: os.environ.get(key) for key in env_values}
            os.environ.update(env_values)
            try:
                rc = full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--action-entry", str(action),
                    "--runtest-entry", str(runtest),
                    "--structure-entry", str(structure),
                    "--judge-entry", str(judge),
                ])
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertEqual(rc, 0)
            report = json.loads((base / "run" / "full_pipeline_report.json").read_text(encoding="utf-8"))
            stages = {stage["name"]: stage for stage in report["stages"]}
            self.assertEqual(stages["action_semantics_by_session"]["summary"]["total"], 1)
            self.assertEqual(stages["aggregate_sessions"]["status"], "completed")
            self.assertEqual(stages["aggregate_sessions"]["items"][0]["replaced"], 1)
            self.assertEqual(stages["runtest_pipeline_by_task"]["status"], "completed")
            judge_snapshot = json.loads(judge_snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(judge_snapshot, {"1-session.json": "action-repaired\n"})
            runtest_snapshot = json.loads(runtest_snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(runtest_snapshot, {
                "1-session.json": "action-repaired\n",
                "2-session.json": "original-two\n",
            })
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["one"], "action-repaired\n")
            self.assertEqual(snapshot["two"], "original-two\n")
            self.assertTrue(snapshot["runtest_complete"])

    def test_from_batch_before_extracts_local_task_zip_without_platform_claim(self) -> None:
        hook_source = '''
from pathlib import Path
import os

Path(os.environ["LOCAL_HOOK_MARKER"]).write_text(os.environ["PIPELINE_STAGE"], encoding="utf-8")
raise SystemExit(9)
'''
        fake_entry_source = '''
import json
import sys
from pathlib import Path

if "--merged-out" in sys.argv:
    output = Path(sys.argv[sys.argv.index("--merged-out") + 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    task = Path(sys.argv[sys.argv.index("--task") + 1])
    sessions = sorted((task / "sessions").rglob("*.json"))
    output.write_text("".join(json.dumps({"session_id": path.stem}) + "\\n" for path in sessions), encoding="utf-8")
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            before = root / "batch_before_tasks"
            before.mkdir(parents=True)
            task_id = "TASK-LOCAL-001"
            archive = before / f"{task_id}.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(f"{task_id}/sessions/one.json", '{"messages": []}\n')

            hook = base / "must-not-run.py"
            hook.write_text(hook_source, encoding="utf-8")
            entries = {}
            for name in ("action", "structure", "judge"):
                path = base / f"{name}.py"
                path.write_text(fake_entry_source, encoding="utf-8")
                entries[name] = path
            command = json.dumps([sys.executable, str(hook)])
            config = base / "pipeline.toml"
            config.write_text(
                "\n".join([
                    "config_version = 1",
                    "[general]",
                    'task_glob = "TASK-*"',
                    'session_glob = "**/*.json"',
                    "[action]",
                    "workers = 1",
                    "[structure]",
                    "check_workers = 1",
                    "fix_workers = 1",
                    "[judge]",
                    "enabled = true",
                    "extra_args = []",
                    "[hooks.claim_tasks]",
                    "enabled = true",
                    f"command = {command}",
                    "[hooks.fetch_from_oss]",
                    "enabled = true",
                    f"command = {command}",
                    "[hooks.mark_qa_complete]",
                    "enabled = true",
                    f"command = {command}",
                ]) + "\n",
                encoding="utf-8",
            )
            marker = base / "local-hook-marker"
            previous = os.environ.get("LOCAL_HOOK_MARKER")
            os.environ["LOCAL_HOOK_MARKER"] = str(marker)
            try:
                rc = full_pipeline.main([
                    "--root", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--from-batch-before",
                    "--action-entry", str(entries["action"]),
                    "--structure-entry", str(entries["structure"]),
                    "--judge-entry", str(entries["judge"]),
                ])
            finally:
                if previous is None:
                    os.environ.pop("LOCAL_HOOK_MARKER", None)
                else:
                    os.environ["LOCAL_HOOK_MARKER"] = previous

            self.assertEqual(rc, 0)
            report = json.loads((base / "run" / "full_pipeline_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["task_ids"], [task_id])
            stages = {stage["name"]: stage for stage in report["stages"]}
            self.assertEqual(stages["claim_tasks"]["status"], "skipped")
            self.assertEqual(stages["fetch_from_oss"]["status"], "completed")
            self.assertEqual(stages["fetch_from_oss"]["summary"]["source"], "local-before")
            self.assertFalse(marker.exists())
            self.assertTrue((root / "success_task" / task_id / "sessions" / "one.json").is_file())

    def test_reserved_hooks_exchange_context_and_claimed_task_ids(self) -> None:
        hook_source = '''
import json
import os
import shutil
from pathlib import Path

stage = os.environ["PIPELINE_STAGE"]
context = json.loads(Path(os.environ["PIPELINE_CONTEXT_PATH"]).read_text(encoding="utf-8"))
records = Path(os.environ["FAKE_PIPELINE_LOG_DIR"])
records.mkdir(parents=True, exist_ok=True)
(records / f"hook-{stage}.json").write_text(json.dumps({
    "stage": stage,
    "context": context,
    "task_count_env": os.environ.get("PIPELINE_TASK_COUNT"),
}), encoding="utf-8")
if stage == "upload_results" and os.environ.get("FAKE_UPLOAD_FAIL") == "1":
    raise SystemExit(9)
if stage == "upload_results" and not Path(os.environ["PIPELINE_GRADES_PATH"]).is_file():
    raise SystemExit(9)
if stage == "claim_tasks":
    counter = records / "claim-count.txt"
    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(count + 1), encoding="utf-8")
    result = {"task_ids": ["TASK-TEST-001"]}
elif stage == "fetch_from_oss":
    task_id = context["task_ids"][0]
    before = Path(context["before_root"]) / task_id
    full_clean = before / "full_clean"
    aggregate = before / "aggregate"
    for name in ("original_sessions", "convert-sessions", "clean_sessions"):
        (full_clean / name).mkdir(parents=True, exist_ok=True)
    (full_clean / "original_sessions" / "original.json").write_text("original\\n", encoding="utf-8")
    (full_clean / "convert-sessions" / "convert.json").write_text("convert\\n", encoding="utf-8")
    (full_clean / "clean_sessions" / "one.json").write_text("clean\\n", encoding="utf-8")
    (aggregate / "sessions").mkdir(parents=True, exist_ok=True)
    (aggregate / "sessions" / "one.json").write_text("aggregate\\n", encoding="utf-8")
    after = Path(context["after_root"]) / task_id
    if after.exists():
        shutil.rmtree(after)
    shutil.copytree(full_clean, after)
    shutil.rmtree(after / "sessions", ignore_errors=True)
    shutil.copytree(aggregate / "sessions", after / "sessions")
    result = {"results": [{
        "task_id": task_id,
        "status": "prepared",
        "full_clean_task_path": str(full_clean),
        "aggregate_task_path": str(aggregate),
        "after_task_path": str(after),
        "action_task_path": str(after),
        "action_sessions": [str(after / "sessions" / "one.json")],
    }]}
else:
    result = {"stage": stage}
result_path = Path(os.environ["PIPELINE_HOOK_RESULT_PATH"])
result_path.parent.mkdir(parents=True, exist_ok=True)
result_path.write_text(json.dumps(result), encoding="utf-8")
'''
        fake_entry_source = '''
import os
import sys
from pathlib import Path

if Path(__file__).stem == "judge":
    out = Path(sys.argv[sys.argv.index("--merged-out") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('{"ok": true}\\n', encoding="utf-8")
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, _sessions = self.make_task_root(base, session_count=1)
            self.write_operator(root, operator_code="user-7")
            hook = base / "hook.py"
            hook.write_text(hook_source, encoding="utf-8")
            entries = {}
            for name in ("action", "structure", "judge"):
                path = base / f"{name}.py"
                path.write_text(fake_entry_source, encoding="utf-8")
                entries[name] = path
            command = json.dumps([sys.executable, str(hook)])
            config = base / "pipeline.toml"
            config.write_text(
                "\n".join([
                    "config_version = 1",
                    "[general]",
                    'task_glob = "TASK-*"',
                    'session_glob = "**/*.json"',
                    'env_file = ".env"',
                    "action_session_workers = 1",
                    "structure_task_workers = 1",
                    "judge_session_workers = 1",
                    "[action]",
                    "workers = 1",
                    "extra_args = []",
                    "[structure]",
                    "check_workers = 1",
                    "fix_workers = 1",
                    "extra_args = []",
                    "[judge]",
                    "enabled = true",
                    "extra_args = []",
                    *[
                        f"[hooks.{name}]\nenabled = true\ncommand = {command}"
                        for name in (
                            "claim_tasks",
                            "fetch_from_oss",
                            "upload_results",
                            "mark_qa_complete",
                            "notify_failure",
                        )
                    ],
                ]) + "\n",
                encoding="utf-8",
            )
            records = base / "hook-records"
            old_value = os.environ.get("FAKE_PIPELINE_LOG_DIR")
            os.environ["FAKE_PIPELINE_LOG_DIR"] = str(records)
            try:
                rc = full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--count", "1",
                    "--action-entry", str(entries["action"]),
                    "--structure-entry", str(entries["structure"]),
                    "--judge-entry", str(entries["judge"]),
                ])
            finally:
                if old_value is None:
                    os.environ.pop("FAKE_PIPELINE_LOG_DIR", None)
                else:
                    os.environ["FAKE_PIPELINE_LOG_DIR"] = old_value
            self.assertEqual(rc, 0)
            report = json.loads((base / "run" / "full_pipeline_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["task_ids"], ["TASK-TEST-001"])
            self.assertEqual(
                [stage["status"] for stage in report["stages"]],
                ["completed"] * 5
                + ["skipped"]
                + ["completed"] * 3
                + ["skipped"] * 3,
            )
            for hook_name in full_pipeline.HOOK_STAGES - {
                "mark_qa_complete",
                "notify_failure",
            }:
                self.assertTrue((records / f"hook-{hook_name}.json").is_file())
            self.assertFalse((records / "hook-mark_qa_in_progress.json").exists())
            self.assertFalse((records / "hook-mark_qa_complete.json").exists())
            self.assertEqual(
                (root / "tasks.txt").read_text(encoding="utf-8"),
                "TASK-TEST-001\n",
            )
            claim_record = json.loads(
                (records / "hook-claim_tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(claim_record["context"]["task_count"], 1)
            self.assertEqual(claim_record["context"]["user_id"], "user-7")
            self.assertEqual(claim_record["context"]["operator_code"], "user-7")
            self.assertEqual(claim_record["task_count_env"], "1")
            upload_context = json.loads(
                (records / "hook-upload_results.json").read_text(encoding="utf-8")
            )["context"]
            self.assertEqual(Path(upload_context["root"]), root / "success_task")
            self.assertEqual(
                Path(upload_context["task_paths"][0]),
                root / "success_task" / "TASK-TEST-001",
            )

            interrupted_report_path = base / "run" / "full_pipeline_report.json"
            interrupted_report = json.loads(
                interrupted_report_path.read_text(encoding="utf-8")
            )
            interrupted_report["status"] = "interrupted"
            interrupted_report_path.write_text(
                json.dumps(interrupted_report),
                encoding="utf-8",
            )
            resumed_rc = full_pipeline.main([
                "-r", str(root),
                "--config", str(config),
                "--run-dir", str(base / "run"),
                "--resume",
            ])
            self.assertEqual(resumed_rc, 0)
            self.assertEqual(
                (records / "claim-count.txt").read_text(encoding="utf-8"),
                "1",
            )

            failed_records = base / "failed-hook-records"
            old_log_dir = os.environ.get("FAKE_PIPELINE_LOG_DIR")
            old_fail = os.environ.get("FAKE_UPLOAD_FAIL")
            os.environ["FAKE_PIPELINE_LOG_DIR"] = str(failed_records)
            os.environ["FAKE_UPLOAD_FAIL"] = "1"
            try:
                failed_rc = full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "failed-run"),
                    "--count", "1",
                    "--action-entry", str(entries["action"]),
                    "--structure-entry", str(entries["structure"]),
                    "--judge-entry", str(entries["judge"]),
                ])
            finally:
                if old_log_dir is None:
                    os.environ.pop("FAKE_PIPELINE_LOG_DIR", None)
                else:
                    os.environ["FAKE_PIPELINE_LOG_DIR"] = old_log_dir
                if old_fail is None:
                    os.environ.pop("FAKE_UPLOAD_FAIL", None)
                else:
                    os.environ["FAKE_UPLOAD_FAIL"] = old_fail
            self.assertEqual(failed_rc, 0)
            failed_report = json.loads(
                (base / "failed-run" / "full_pipeline_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(failed_report["status"], "completed")
            self.assertEqual(failed_report["task_summary"]["failed"], 1)
            failed_by_name = {stage["name"]: stage for stage in failed_report["stages"]}
            self.assertEqual(failed_by_name["upload_results"]["status"], "failed")
            self.assertEqual(failed_by_name["mark_qa_complete"]["status"], "skipped")
            self.assertFalse((failed_records / "hook-mark_qa_complete.json").exists())
            notification = json.loads(
                (failed_records / "hook-notify_failure.json").read_text(encoding="utf-8")
            )
            self.assertEqual(notification["context"]["task_ids"], ["TASK-TEST-001"])
            failed_state = notification["context"]["task_states"]["TASK-TEST-001"]
            self.assertEqual(failed_state["status"], "failed")
            self.assertEqual(failed_state["failed_stage"], "upload_results")
            self.assertFalse((root / "success_task" / "TASK-TEST-001").exists())
            self.assertTrue((root / "fail_task" / "TASK-TEST-001" / "sessions" / "one.json").is_file())

    def test_claim_failure_stops_without_fallback_or_retry(self) -> None:
        claim_source = '''
from pathlib import Path
import os

counter = Path(os.environ["CLAIM_COUNTER"])
count = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(count + 1), encoding="utf-8")
raise SystemExit(9)
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, _sessions = self.make_task_root(base, session_count=1)
            self.write_operator(root)
            claim = base / "claim.py"
            claim.write_text(claim_source, encoding="utf-8")
            config = base / "pipeline.toml"
            config.write_text(
                "\n".join([
                    "config_version = 1",
                    "[general]",
                    'task_glob = "TASK-*"',
                    'session_glob = "**/*.json"',
                    "command_retries = 1",
                    "[action]",
                    'entry = "action_semantics_fix.py"',
                    "[structure]",
                    'entry = "structure_check_fix.py"',
                    "[judge]",
                    'entry = "action_semantics_check_script/seven_dimension_quality/detector/batch_judge_tasks.py"',
                    "[hooks.claim_tasks]",
                    "enabled = true",
                    "retries = 5",
                    f"command = {json.dumps([sys.executable, str(claim)])}",
                    "[hooks.fetch_from_oss]",
                    "enabled = true",
                    f"command = {json.dumps([sys.executable, str(claim)])}",
                ]) + "\n",
                encoding="utf-8",
            )
            counter = base / "claim-count.txt"
            previous = os.environ.get("CLAIM_COUNTER")
            os.environ["CLAIM_COUNTER"] = str(counter)
            try:
                rc = full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--count", "1",
                    "--continue-on-error",
                ])
            finally:
                if previous is None:
                    os.environ.pop("CLAIM_COUNTER", None)
                else:
                    os.environ["CLAIM_COUNTER"] = previous
            self.assertEqual(rc, 1)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")
            report = json.loads((base / "run" / "full_pipeline_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual([stage["name"] for stage in report["stages"]], ["claim_tasks"])
            self.assertEqual(report["stages"][0]["items"][0]["attempts"], 1)

    def test_count_mode_requires_operator_and_enabled_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, _sessions = self.make_task_root(base, session_count=1)
            config = base / "claim-only.toml"
            config.write_text(
                "\n".join([
                    "config_version = 1",
                    "[hooks.claim_tasks]",
                    "enabled = true",
                    f"command = {json.dumps([sys.executable, str(base / 'unused.py')])}",
                ]) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FileNotFoundError, "operator.json"):
                full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--count", "1",
                ])
            self.write_operator(root)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run-invalid-count"),
                    "--count", "0",
                ])
            with self.assertRaisesRegex(ValueError, "requires enabled fetch_from_oss"):
                full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run-2"),
                    "--count", "1",
                ])
            disabled = base / "disabled.toml"
            disabled.write_text("config_version = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "requires enabled claim_tasks"):
                full_pipeline.main([
                    "-r", str(root),
                    "--config", str(disabled),
                    "--run-dir", str(base / "run-3"),
                    "--count", "1",
                ])

    def test_task_ids_do_not_accept_local_paths(self) -> None:
        with self.assertRaises(ValueError):
            full_pipeline.validate_claim_task_ids(["..\\outside"])
        with self.assertRaises(ValueError):
            full_pipeline.validate_claim_task_ids([str(Path.cwd())])

    def test_pipeline_buckets_stay_inside_root_and_do_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "must be inside"):
                full_pipeline.validate_workspace_roots(root, {
                    "before_tasks": root / "batch_before_tasks",
                    "after_tasks": root.parent / "outside",
                    "success_tasks": root / "success_task",
                    "fail_tasks": root / "fail_task",
                })
            with self.assertRaisesRegex(ValueError, "must not overlap"):
                full_pipeline.validate_workspace_roots(root, {
                    "before_tasks": root / "batch_before_tasks",
                    "after_tasks": root / "batch_before_tasks" / "nested",
                    "success_tasks": root / "success_task",
                    "fail_tasks": root / "fail_task",
                })

    def test_fetch_hook_paths_cannot_escape_canonical_task_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            before = base / "batch_before_tasks"
            after = base / "batch_after_tasks"
            with self.assertRaisesRegex(ValueError, "action_task_path"):
                full_pipeline.validate_fetch_artifact_paths(
                    {
                        "full_clean_task_path": str(before / "TASK-ONE" / "full_clean"),
                        "aggregate_task_path": str(before / "TASK-ONE" / "aggregate"),
                        "after_task_path": str(after / "TASK-ONE"),
                        "action_task_path": str(base / "outside" / "TASK-ONE"),
                    },
                    "TASK-ONE",
                    before,
                    after,
                )

    def test_online_fetch_requires_complete_prepared_task_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            before = base / "batch_before_tasks"
            after = base / "batch_after_tasks"
            task = "TASK-ONE"
            full_clean = before / task / "full_clean"
            aggregate = before / task / "aggregate"
            after_task = after / task
            for path in (
                full_clean / "sessions",
                aggregate / "sessions",
                after_task / "sessions",
            ):
                path.mkdir(parents=True)
            session = after_task / "sessions" / "one.json"
            session.write_text("{}\n", encoding="utf-8")
            result = full_pipeline.validate_online_fetch_artifact(
                {
                    "full_clean_task_path": str(full_clean),
                    "aggregate_task_path": str(aggregate),
                    "after_task_path": str(after_task),
                    "action_task_path": str(after_task),
                    "action_sessions": [str(session)],
                },
                task,
                before,
                after,
            )
            self.assertEqual(result["action_sessions"], [str(session.resolve())])
            with self.assertRaisesRegex(ValueError, "required path"):
                full_pipeline.validate_online_fetch_artifact(
                    {"after_task_path": str(after_task)}, task, before, after
                )

    def test_skipped_required_stage_blocks_upload_and_platform_completion(self) -> None:
        hook_source = '''
import json
import os
import shutil
from pathlib import Path

stage = os.environ["PIPELINE_STAGE"]
context = json.loads(Path(os.environ["PIPELINE_CONTEXT_PATH"]).read_text(encoding="utf-8"))
records = Path(os.environ["GATE_HOOK_RECORDS"])
records.mkdir(parents=True, exist_ok=True)
(records / f"{stage}.txt").write_text(stage, encoding="utf-8")
if stage == "claim_tasks":
    payload = {"task_ids": ["TASK-TEST-001"]}
elif stage == "fetch_from_oss":
    task_id = context["task_ids"][0]
    before = Path(context["before_root"]) / task_id
    full_clean = before / "full_clean"
    aggregate = before / "aggregate"
    for name in ("original_sessions", "convert-sessions", "clean_sessions"):
        (full_clean / name).mkdir(parents=True, exist_ok=True)
    (full_clean / "original_sessions" / "original.json").write_text("original\\n", encoding="utf-8")
    (full_clean / "convert-sessions" / "convert.json").write_text("convert\\n", encoding="utf-8")
    (full_clean / "clean_sessions" / "one.json").write_text("clean\\n", encoding="utf-8")
    (aggregate / "sessions").mkdir(parents=True, exist_ok=True)
    (aggregate / "sessions" / "one.json").write_text("aggregate\\n", encoding="utf-8")
    after = Path(context["after_root"]) / task_id
    if after.exists():
        shutil.rmtree(after)
    shutil.copytree(full_clean, after)
    shutil.copytree(aggregate / "sessions", after / "sessions")
    payload = {"results": [{
        "task_id": task_id,
        "status": "prepared",
        "full_clean_task_path": str(full_clean),
        "aggregate_task_path": str(aggregate),
        "after_task_path": str(after),
        "action_task_path": str(after),
        "action_sessions": [str(after / "sessions" / "one.json")],
    }]}
else:
    payload = {"stage": stage}
result = Path(os.environ["PIPELINE_HOOK_RESULT_PATH"])
result.parent.mkdir(parents=True, exist_ok=True)
result.write_text(json.dumps(payload), encoding="utf-8")
'''
        fake_entry_source = '''
import json
import sys
from pathlib import Path

if "--merged-out" in sys.argv:
    out = Path(sys.argv[sys.argv.index("--merged-out") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ok": True}) + "\\n", encoding="utf-8")
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _task, _sessions = self.make_task_root(base, session_count=1)
            self.write_operator(root)
            hook = base / "hook.py"
            hook.write_text(hook_source, encoding="utf-8")
            entries = {}
            for name in ("action", "runtest", "structure", "judge"):
                path = base / f"{name}.py"
                path.write_text(fake_entry_source, encoding="utf-8")
                entries[name] = path
            command = json.dumps([sys.executable, str(hook)])
            config = base / "pipeline.toml"
            config.write_text(
                "\n".join([
                    "config_version = 1",
                    "[general]",
                    'task_glob = "TASK-*"',
                    'session_glob = "**/*.json"',
                    "completion_requires_upload = false",
                    "[action]",
                    "workers = 1",
                    "[runtest]",
                    "enabled = true",
                    "workers = 1",
                    "[structure]",
                    "check_workers = 1",
                    "fix_workers = 1",
                    "[judge]",
                    "enabled = true",
                    "extra_args = []",
                    *[
                        f"[hooks.{name}]\nenabled = true\nretries = 0\ncommand = {command}"
                        for name in (
                            "claim_tasks",
                            "fetch_from_oss",
                            "upload_results",
                            "mark_qa_complete",
                        )
                    ],
                ]) + "\n",
                encoding="utf-8",
            )
            previous = os.environ.get("GATE_HOOK_RECORDS")
            try:
                for blocked_stage in (
                    "quality_judge",
                    "aggregate_sessions",
                    "runtest_pipeline_by_task",
                ):
                    with self.subTest(blocked_stage=blocked_stage):
                        records = base / f"hook-records-{blocked_stage}"
                        os.environ["GATE_HOOK_RECORDS"] = str(records)
                        run_dir = base / f"run-{blocked_stage}"
                        rc = full_pipeline.main([
                            "-r", str(root),
                            "--config", str(config),
                            "--run-dir", str(run_dir),
                            "--count", "1",
                            "--action-entry", str(entries["action"]),
                            "--runtest-entry", str(entries["runtest"]),
                            "--structure-entry", str(entries["structure"]),
                            "--judge-entry", str(entries["judge"]),
                            "--skip", blocked_stage,
                        ])
                        self.assertEqual(rc, 0)
                        self.assertFalse((records / "upload_results.txt").exists())
                        self.assertFalse((records / "mark_qa_complete.txt").exists())
                        report = json.loads(
                            (run_dir / "full_pipeline_report.json").read_text(encoding="utf-8")
                        )
                        state = report["task_states"]["TASK-TEST-001"]
                        self.assertEqual(state["status"], "failed")
                        self.assertEqual(state["failed_stage"], "pre_upload_gate")
                        self.assertIn(blocked_stage, state["error"])
                        by_name = {stage["name"]: stage for stage in report["stages"]}
                        self.assertEqual(by_name["upload_results"]["status"], "skipped")
                        self.assertEqual(by_name["mark_qa_complete"]["status"], "skipped")
            finally:
                if previous is None:
                    os.environ.pop("GATE_HOOK_RECORDS", None)
                else:
                    os.environ["GATE_HOOK_RECORDS"] = previous

    def test_failed_task_does_not_block_a_successful_task(self) -> None:
        class RecordingDatabase:
            instance = None

            def __init__(self, _settings) -> None:
                self.completed: list[list[str]] = []
                self.failed: list[list[str]] = []
                RecordingDatabase.instance = self

            def ensure_task_rows(self, *_args, **_kwargs) -> None:
                pass

            def mark_running(self, _task_ids) -> None:
                pass

            def mark_stage_flag(self, _task_ids, _column, _value) -> None:
                pass

            def set_task_sub_status(self, _task_ids, _status, _status_name) -> None:
                pass

            def mark_finished(self, _task_ids) -> None:
                pass

            def mark_completed(self, task_ids) -> None:
                self.completed.append(list(task_ids))

            def mark_failed(self, task_ids) -> None:
                self.failed.append(list(task_ids))

            def close(self) -> None:
                pass

        fake_source = '''
import json
import sys
from pathlib import Path

kind = Path(__file__).stem
if kind == "action" and any("TASK-FAIL" in arg for arg in sys.argv[1:]):
    raise SystemExit(5)
if kind == "judge":
    task = Path(sys.argv[sys.argv.index("--task") + 1])
    out = Path(sys.argv[sys.argv.index("--merged-out") + 1])
    sessions = sorted((task / "sessions").rglob("*.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps({"task_id": task.name, "session_id": p.stem}) + "\\n" for p in sessions), encoding="utf-8")
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "tasks"
            for task_id in ("TASK-GOOD", "TASK-FAIL"):
                sessions = root / task_id / "sessions"
                sessions.mkdir(parents=True)
                (sessions / "one.json").write_text('{"messages": []}\n', encoding="utf-8")
            entries = {}
            for name in ("action", "structure", "judge"):
                path = base / f"{name}.py"
                path.write_text(fake_source, encoding="utf-8")
                entries[name] = path
            config = base / "pipeline.toml"
            config.write_text(
                "[judge]\n"
                "enabled = true\n"
                "[database]\n"
                "enabled = true\n"
                'host = "127.0.0.1"\n'
                'database = "demo"\n'
                'user = "demo"\n'
                'password = "secret"\n',
                encoding="utf-8",
            )
            original_database = full_pipeline.TaskStructureSemanticsDatabase
            full_pipeline.TaskStructureSemanticsDatabase = RecordingDatabase
            try:
                rc = full_pipeline.main([
                    "-r", str(root),
                    "--config", str(config),
                    "--run-dir", str(base / "run"),
                    "--action-entry", str(entries["action"]),
                    "--structure-entry", str(entries["structure"]),
                    "--judge-entry", str(entries["judge"]),
                    "--action-session-workers", "2",
                    "--skip", ",".join(sorted(full_pipeline.HOOK_STAGES)),
                ])
            finally:
                full_pipeline.TaskStructureSemanticsDatabase = original_database
            self.assertEqual(rc, 0)
            report = json.loads((base / "run" / "full_pipeline_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["task_summary"]["completed"], 1)
            self.assertEqual(report["task_summary"]["failed"], 1)
            self.assertEqual(report["task_states"]["TASK-GOOD"]["status"], "completed")
            self.assertEqual(report["task_states"]["TASK-FAIL"]["status"], "failed")
            action = next(stage for stage in report["stages"] if stage["name"] == "action_semantics_by_session")
            structure = next(stage for stage in report["stages"] if stage["name"] == "structure_check_fix_by_task")
            judge = next(stage for stage in report["stages"] if stage["name"] == "quality_judge")
            self.assertEqual(
                {item["item"].split("::", 1)[0] for item in action["items"]},
                {"TASK-GOOD", "TASK-FAIL"},
            )
            self.assertEqual({item["item"] for item in structure["items"]}, {"TASK-GOOD"})
            self.assertEqual(
                {item["item"] for item in judge["items"]},
                {"TASK-GOOD"},
            )
            self.assertTrue((root / "success_task" / "TASK-GOOD" / "sessions" / "one.json").is_file())
            self.assertTrue((root / "fail_task" / "TASK-FAIL" / "sessions" / "one.json").is_file())
            self.assertEqual(RecordingDatabase.instance.completed, [["TASK-GOOD"]])
            self.assertEqual(RecordingDatabase.instance.failed, [["TASK-FAIL"]])

    def test_run_lock_and_command_retry_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            lock_path = base / "pipeline.lock"
            first = full_pipeline.PipelineLock(lock_path)
            second = full_pipeline.PipelineLock(lock_path)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()

            counter = base / "counter.txt"
            retry_script = base / "retry.py"
            retry_script.write_text(
                "from pathlib import Path\n"
                f"p=Path(r'{counter}')\n"
                "n=int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(n+1))\n"
                "raise SystemExit(5 if n == 0 else 0)\n",
                encoding="utf-8",
            )
            result = full_pipeline.execute_command(
                "retry", [sys.executable, str(retry_script)], base / "retry.log", dict(os.environ), False, 10, 1
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.attempts, 2)

            timeout_script = base / "timeout.py"
            timeout_script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
            result = full_pipeline.execute_command(
                "timeout", [sys.executable, str(timeout_script)], base / "timeout.log", dict(os.environ), False, 1, 0
            )
            self.assertEqual(result.status, "timeout")
            self.assertEqual(result.returncode, 124)

    def test_run_retention_only_prunes_completed_old_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "pipeline_runs"
            current = parent / "current"
            incomplete = parent / "incomplete"
            old_runs = [parent / f"old-{index}" for index in range(3)]
            for path in [current, incomplete, *old_runs]:
                path.mkdir(parents=True)
            for index, path in enumerate(old_runs):
                (path / "full_pipeline_report.json").write_text("{}\n", encoding="utf-8")
                timestamp = 1_000 + index
                os.utime(path, (timestamp, timestamp))

            full_pipeline.prune_runs(parent, current, keep=2)

            self.assertTrue(current.is_dir())
            self.assertTrue(incomplete.is_dir())
            self.assertTrue(old_runs[2].is_dir())
            self.assertFalse(old_runs[0].exists())
            self.assertFalse(old_runs[1].exists())


if __name__ == "__main__":
    unittest.main()
